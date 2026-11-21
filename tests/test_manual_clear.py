"""Manual clearing through real writer/store, isolated SQLite and mocked notifiers."""
import json
import queue
import threading
import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from gateway.chirpstack import build_uplink, uplink_topic
from gateway.codec import SensorReading, encode, encode_control
from gateway.commands import CommandSettings, ClearRequest, start_command_server, validate_command
from gateway.db import events, nodes, readings, make_engine
from gateway.detect import DetectionEngine
from gateway.ingest import Ingestor
from gateway.service import _replay
from gateway.store import Store
from gateway.web.app import create_app
from gateway.web import commands as web_commands
from tests.test_service import CFG, START, make_pipeline, close, Recorder

TOKEN = 'test_only_token_' + 'x' * 32


@pytest.fixture(autouse=True)
def no_operator_environment(monkeypatch):
    monkeypatch.delenv('WILDFIRE_LOCAL_DEMO', raising=False)
    monkeypatch.delenv('WILDFIRE_ADMIN_TOKEN', raising=False)
    monkeypatch.delenv('WILDFIRE_COMMAND_PORT', raising=False)


def send(p, kind='FIRE_ALERT', at=START, counter=1, index=0, reading=None):
    n = CFG.simulator.nodes[index]
    body = build_uplink(application_id=CFG.simulator.application_id, application_name='test',
        device_name=n.name, dev_eui=n.dev_eui, dev_addr=n.dev_addr, f_cnt=counter, f_port=3,
        payload=encode(reading) if reading else encode_control(kind), rssi=-90, snr=7,
        time=at, gateway_id=CFG.simulator.gateway_id)
    got = Ingestor().accept(uplink_topic(CFG.simulator.application_id,n.dev_eui), json.dumps(body).encode(), at)
    p.on_uplink(got, at)
    return got


def alarm_id(db, node):
    with db.connect() as conn:
        return conn.scalar(select(func.max(events.c.id)).where(events.c.dev_eui==node, events.c.to_state=='ALARM'))


def finish(p, command):
    assert p.writer.flush(5)
    p.finish_clears()
    assert p.writer.flush(5)
    return command.result.result(timeout=1)


@pytest.fixture
def pipeline(engine):
    recorder = Recorder()
    p = make_pipeline(Store(engine), DetectionEngine(CFG.detection), [recorder])
    yield p, recorder
    close(p)


def test_clear_offline_isolated_audited_restart_and_rearm(engine, pipeline):
    p, recorder = pipeline
    first = send(p)
    send(p, index=1)
    assert p.writer.flush(5)
    node = first.uplink.dev_eui
    old_id = alarm_id(engine, node)
    at = START + timedelta(days=4)
    p.check_offline(at)
    assert p.writer.flush(5)
    before = p.engine.nodes[node].last_contact
    req = ClearRequest(node, old_id)
    p.request_clear(req, at)
    status, result = finish(p, req)
    assert status == 200 and result['cleared']
    assert p.engine.nodes[node].state == 'NORMAL'
    assert not p.engine.nodes[node].online
    assert p.engine.nodes[node].last_contact == before
    assert p.engine.nodes[CFG.simulator.nodes[1].dev_eui].state == 'ALARM'
    # The same confirmation is safe to repeat and creates no additional audit.
    req = ClearRequest(node, old_id)
    p.request_clear(req, at)
    assert finish(p, req)[1]['cleared'] is False
    with engine.connect() as conn:
        audit = conn.execute(select(events).where(events.c.kind=='MANUAL_CLEAR')).all()
        assert len(audit) == 1 and audit[0].details['alarm_event_id'] == old_id
        assert conn.execute(select(events).where(events.c.id==old_id)).one().to_state == 'ALARM'
        assert conn.scalar(select(func.count()).select_from(readings)) == 0
        row = conn.execute(select(nodes).where(nodes.c.dev_eui==node)).one()
        assert not row.online and not row.explicit_fire_active
    restarted = DetectionEngine(CFG.detection)
    _replay(CFG, Store(engine), restarted, now=at)
    assert restarted.nodes[node].state == 'NORMAL'
    assert not restarted.nodes[node].explicit_fire_active
    assert not restarted.nodes[node].online
    p.engine = restarted
    # Exact duplicate and previously unseen delayed pre-clear alert cannot re-latch.
    p.on_uplink(first, at)
    send(p, counter=2, at=START+timedelta(minutes=1))
    assert p.engine.nodes[node].state == 'NORMAL'
    send(p, counter=3, at=at+timedelta(minutes=1))
    assert p.engine.nodes[node].explicit_fire_active
    assert p.writer.flush(5)
    assert alarm_id(engine, node) != old_id
    req = ClearRequest(node, old_id)
    p.request_clear(req, at+timedelta(minutes=2))
    assert finish(p, req)[0] == 409
    assert p.engine.nodes[node].state == 'ALARM'
    send(p, kind='HEARTBEAT', counter=4, at=at+timedelta(minutes=3))
    assert p.engine.nodes[node].state == 'ALARM'


def test_new_alert_during_commit_is_not_lost(engine, pipeline):
    p, rec = pipeline
    first = send(p)
    assert p.writer.flush(5)
    req = ClearRequest(first.uplink.dev_eui, alarm_id(engine, first.uplink.dev_eui))
    p.request_clear(req, START+timedelta(minutes=1))
    newer = send(p, counter=2, at=START+timedelta(minutes=2))
    assert len(p.deferred_uplinks[first.uplink.dev_eui]) == 1
    assert finish(p, req)[0] == 200
    assert p.engine.nodes[first.uplink.dev_eui].explicit_fire_active
    p.on_uplink(newer, START+timedelta(minutes=3))
    assert p.writer.flush(5)
    p.dispatcher.stop()
    assert len(rec.sent) == 2  # No clear notification and no duplicate new alert.


def test_clear_failure_expiry_unknown_and_commit_ack_loss(engine, pipeline, monkeypatch):
    p, _ = pipeline
    got = send(p)
    assert p.writer.flush(5)
    node = got.uplink.dev_eui
    old_id = alarm_id(engine, node)
    unknown = ClearRequest('ffffffffffffffff', old_id)
    p.request_clear(unknown, START)
    assert unknown.result.result()[0] == 404
    expired = ClearRequest(node, old_id, deadline=0)
    p.request_clear(expired, START)
    assert expired.result.result()[0] == 503
    original = p.writer.store.clear_alarm
    def fail(*args):
        raise RuntimeError('simulated storage failure')
    monkeypatch.setattr(p.writer.store, 'clear_alarm', fail)
    request = ClearRequest(node, old_id)
    p.request_clear(request, START+timedelta(minutes=1))
    assert finish(p, request)[0] == 503
    assert p.engine.nodes[node].explicit_fire_active
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(events).where(events.c.kind=='MANUAL_CLEAR')) == 0
    attempts = []
    def lose_ack(*args):
        result = original(*args)
        attempts.append(True)
        if len(attempts)==1:
            raise OperationalError('test', {}, Exception('lost commit acknowledgment'))
        return result
    monkeypatch.setattr(p.writer.store, 'clear_alarm', lose_ack)
    request = ClearRequest(node, old_id)
    p.request_clear(request, START+timedelta(minutes=2))
    assert finish(p, request)[0] == 200
    assert len(attempts)==2
    assert not p.engine.nodes[node].explicit_fire_active


def test_legacy_clear_preserves_readings_after_replay(engine, pipeline):
    p, _ = pipeline
    for i in range(60):
        fire = i >= 35
        r = SensorReading(28 if fire else 20, 35 if fire else 50, 1000, 50000 if fire else 200000, 3.6)
        got = send(p, at=START+timedelta(minutes=i), counter=i, reading=r)
    assert p.writer.flush(5)
    node = got.uplink.dev_eui
    assert p.engine.nodes[node].state == 'ALARM'
    req = ClearRequest(node, alarm_id(engine,node))
    at = START+timedelta(hours=2)
    p.request_clear(req, at)
    assert finish(p,req)[0] == 200
    restarted = DetectionEngine(CFG.detection)
    _replay(CFG, Store(engine), restarted, now=at)
    assert restarted.nodes[node].state == 'NORMAL'
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(readings)) == 60


def test_authorized_web_proxy_readonly_db_and_history(engine, pipeline, monkeypatch):
    p, _ = pipeline
    got = send(p)
    assert p.writer.flush(5)
    node = got.uplink.dev_eui
    old_id = alarm_id(engine,node)
    monkeypatch.setenv('WILDFIRE_ADMIN_TOKEN', TOKEN)
    calls=[]
    def proxy(settings, dev, alarm):
        calls.append(dev)
        req=ClearRequest(dev,alarm)
        p.request_clear(req,START+timedelta(minutes=1))
        return finish(p,req)
    monkeypatch.setattr(web_commands,'forward_clear',proxy)
    # Real SQLite read-only connection: the HTTP process cannot mutate the DB.
    ro = make_engine(str(engine.url), read_only=True) if engine.dialect.name=='sqlite' else engine
    try:
        with TestClient(create_app(CFG,ro),base_url='https://testserver') as client:
            url=f'/api/console/nodes/{node}/clear-alarm'
            body={'alarm_event_id':old_id}
            assert client.post(url,json=body).status_code==401
            assert client.post(url,json=body,headers={'Authorization':'Bearer wrong'}).status_code==401
            auth={'Authorization':f'Bearer {TOKEN}'}
            assert client.post(url,json=body,headers={**auth,'Origin':'https://evil.invalid'}).status_code==403
            assert client.post('/api/console/nodes/bad/clear-alarm',json=body,headers=auth).status_code==422
            assert client.post(url,json={'alarm_event_id':True},headers=auth).status_code==422
            assert not calls
            config=client.get('/api/console/config').json()
            assert config['clear_alarm_enabled'] and TOKEN not in json.dumps(config)
            assert client.post(url,json=body,headers=auth).json()['cleared']
            status=client.get('/api/console/status').json()
            assert status['nodes'][0]['state']=='NORMAL'
            assert status['nodes'][0]['alarm_event_id'] is None
            history=client.get('/api/events').json()['events']
            assert any(e['kind']=='MANUAL_CLEAR' for e in history)
            assert any(e['to_state']=='ALARM' for e in history)
    finally:
        if ro is not engine: ro.dispose()


def test_disabled_unavailable_and_invalid_secret(engine, monkeypatch):
    with TestClient(create_app(CFG,engine)) as client:
        assert not client.get('/api/console/config').json()['clear_alarm_enabled']
        assert client.post('/api/console/nodes/0000000000000001/clear-alarm',json={'alarm_event_id':1}).status_code==503
    monkeypatch.setenv('WILDFIRE_ADMIN_TOKEN','short')
    with pytest.raises(ValueError): CommandSettings.from_env()
    monkeypatch.setenv('WILDFIRE_ADMIN_TOKEN',TOKEN)
    monkeypatch.setattr(web_commands,'forward_clear',lambda *a:(503,{'detail':'Unavailable'}))
    with TestClient(create_app(CFG,engine),base_url='https://testserver') as client:
        response=client.post('/api/console/nodes/0000000000000001/clear-alarm',json={'alarm_event_id':1},
                             headers={'Authorization':f'Bearer {TOKEN}'})
        assert response.status_code==503


def test_loopback_listener_authorization_and_queue():
    inbox=queue.Queue(maxsize=2)
    server=start_command_server(CommandSettings(TOKEN,0),inbox)
    assert server.server_address[0]=='127.0.0.1'
    def processor():
        command=inbox.get(timeout=3)
        command.result.set_result((200,{'ok':True,'cleared':True}))
    worker=threading.Thread(target=processor)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False) as client:
            body={'dev_eui':'0000000000000001','alarm_event_id':1}
            assert client.post('/clear-alarm',json=body).status_code==401
            assert inbox.empty()
            auth={'Authorization':f'Bearer {TOKEN}'}
            assert client.post('/clear-alarm',json={**body,'dev_eui':'bad'},headers=auth).status_code==400
            assert inbox.empty()
            worker.start()
            assert client.post('/clear-alarm',json=body,headers=auth).json()['ok']
        worker.join(3)
    finally:
        server.shutdown();server.server_close()


def test_http_proxy_to_service_writer_end_to_end(engine, pipeline, monkeypatch):
    p, _ = pipeline
    got=send(p)
    assert p.writer.flush(5)
    node=got.uplink.dev_eui
    inbox=queue.Queue(maxsize=2)
    server=start_command_server(CommandSettings(TOKEN,0),inbox)
    monkeypatch.setenv('WILDFIRE_ADMIN_TOKEN',TOKEN)
    monkeypatch.setenv('WILDFIRE_COMMAND_PORT',str(server.server_port))
    def service_loop():
        command=inbox.get(timeout=5)
        p.request_clear(command,START+timedelta(minutes=1))
        until=time.monotonic()+5
        while not command.result.done() and time.monotonic()<until:
            p.finish_clears()
            time.sleep(.005)
    worker=threading.Thread(target=service_loop)
    worker.start()
    try:
        with TestClient(create_app(CFG,engine),base_url='https://testserver') as client:
            response=client.post(f'/api/console/nodes/{node}/clear-alarm',
                json={'alarm_event_id':alarm_id(engine,node)},headers={'Authorization':f'Bearer {TOKEN}'})
            assert response.status_code==200 and response.json()['cleared']
            assert client.get('/api/console/status').json()['nodes'][0]['state']=='NORMAL'
        worker.join(5)
        assert not worker.is_alive()
    finally:
        server.shutdown();server.server_close()


def test_expired_writer_command_does_not_clear(engine, pipeline):
    p, _ = pipeline
    got=send(p)
    assert p.writer.flush(5)
    from gateway.writer import ClearAlarmJob
    import copy
    d=p.engine.nodes[got.uplink.dev_eui]
    request=ClearRequest(d.dev_eui,alarm_id(engine,d.dev_eui),deadline=0)
    job=ClearAlarmJob(request,copy.copy(d).manual_clear(START+timedelta(minutes=1)),None)
    job.run(p.writer.store)
    assert job.result[0]==503 and job.done.is_set()
    assert d.explicit_fire_active
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(events).where(events.c.kind=='MANUAL_CLEAR'))==0
