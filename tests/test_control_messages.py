import base64
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from gateway.codec import CodecError, ControlMessage, decode_message, encode_control
from gateway.db import nodes, events, readings, control_uplinks
from gateway.detect import DetectionEngine
from gateway.ingest import Ingestor
from gateway.service import _replay
from gateway.store import Store
from gateway.web.app import create_app
from tests.test_ingest import make_envelope, TOPIC, NODE, T0, CFG
from tests.test_service import make_pipeline, close
from gateway.notify import Notifier

class Recorder(Notifier):
    name='mock'
    def __init__(self): self.sent=[]
    def send(self,event): self.sent.append(event)


def accepted(ing,kind,counter=1,t=T0):
    env=make_envelope(counter,t,data=base64.b64encode(encode_control(kind)).decode())
    return ing.accept(TOPIC,json.dumps(env).encode(),t)

@pytest.mark.parametrize('payload',[b'',b'\x02',b'\x02\x00',b'\x02\x03',b'\x02\x01\x00',b'\x03\x01'])
def test_bad_control_payload(payload):
    with pytest.raises(CodecError): decode_message(payload)
    env=make_envelope(data=base64.b64encode(payload).decode())
    assert Ingestor().accept(TOPIC,json.dumps(env).encode(),T0) is None

@pytest.mark.parametrize('kind',['HEARTBEAT','FIRE_ALERT'])
def test_control_duplicate_ingestion(kind):
    ing=Ingestor()
    assert accepted(ing,kind).reading == ControlMessage(kind)
    assert accepted(ing,kind) is None
    assert ing.stats.duplicates==1


def test_alert_heartbeat_restart_and_legacy(engine):
    store=Store(engine);det=DetectionEngine(CFG.detection);rec=Recorder()
    p=make_pipeline(store,det,[rec]);ing=Ingestor()
    try:
        # No environmental baseline; first alert must notify immediately.
        alert=accepted(ing,'FIRE_ALERT')
        p.on_uplink(alert,T0)
        p.on_uplink(alert,T0) # Pipeline also guards duplicates independently.
        p.on_uplink(accepted(ing,'HEARTBEAT',2,T0+timedelta(days=1)),T0+timedelta(days=1))
        # Late unique alert is recorded, but cannot regress contact or re-notify a latch.
        p.on_uplink(accepted(ing,'FIRE_ALERT',3,T0-timedelta(hours=1)),T0)
        assert p.writer.flush(5)
    finally: close(p)
    assert len(rec.sent)==1 and rec.sent[0].to_state=='ALARM'
    with engine.connect() as c:
        row=c.execute(select(nodes)).one()
        assert row.explicit_fire_active and row.state=='ALARM'
        assert row.last_seen==T0+timedelta(days=1) and row.last_message_type=='HEARTBEAT'
        assert c.scalar(select(func.count()).select_from(readings))==0
        assert c.scalar(select(func.count()).select_from(control_uplinks))==3
        evs=c.execute(select(events)).all()
        assert len(evs)==2 and evs[0].notified
        assert evs[0].details['message_type']=='FIRE_ALERT'
    restarted=DetectionEngine(CFG.detection)
    _replay(CFG,store,restarted,now=T0+timedelta(days=10))
    d=restarted.nodes[NODE.dev_eui]
    assert d.explicit_fire_active and d.state=='ALARM'
    p=make_pipeline(store,restarted,[rec])
    try:
        p.on_uplink(alert,T0) # restart dedup persists beyond the replay window
        env=make_envelope(4,T0+timedelta(days=2))
        got=Ingestor().accept(TOPIC,json.dumps(env).encode(),T0+timedelta(days=2))
        p.on_uplink(got,T0+timedelta(days=2))
        assert p.writer.flush(5)
    finally: close(p)
    assert len(rec.sent)==1
    with TestClient(create_app(CFG,engine,clock=lambda:T0+timedelta(days=2))) as client:
        s=client.get('/api/console/status').json()
        assert s['pill']=='FIRE_ALARM'
        assert s['nodes'][0]['explicit_fire_active'] is True
        assert len(client.get('/api/events').json()['events'])==2


def test_heartbeat_without_sample_and_out_of_order(engine):
    store=Store(engine);det=DetectionEngine(CFG.detection);p=make_pipeline(store,det)
    try:
        ing=Ingestor()
        p.on_uplink(accepted(ing,'HEARTBEAT',1),T0)
        p.on_uplink(accepted(ing,'HEARTBEAT',2,T0-timedelta(days=1)),T0)
        assert p.writer.flush(5)
    finally: close(p)
    with engine.connect() as c:
        n=c.execute(select(nodes)).one()
        assert n.last_seen==T0 and n.online and n.state=='NORMAL'
        assert c.scalar(select(func.count()).select_from(readings))==0
        assert c.scalar(select(func.count()).select_from(events))==0
    d=det.nodes[NODE.dev_eui]
    assert d.check_offline(T0+timedelta(days=2)) is None
    assert d.check_offline(T0+timedelta(days=4)) is not None


def test_additive_upgrade_preserves_history(tmp_path):
    from sqlalchemy import MetaData, Table, Column, insert
    from gateway.db import init_schema, make_engine, metadata
    db=make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    old=MetaData()
    old_nodes=Table('nodes',old,*[
        Column(c.name,c.type,primary_key=c.primary_key,nullable=c.nullable)
        for c in nodes.columns if c.name not in ('last_message_type','explicit_fire_active')])
    old.create_all(db)
    with db.begin() as c:
        c.execute(insert(old_nodes).values(dev_eui=NODE.dev_eui,name=NODE.name,
            first_seen=T0,last_seen=T0,state='NORMAL',online=True))
    init_schema(db)
    init_schema(db) # repeat startup is safe
    with db.connect() as c:
        row=c.execute(select(nodes)).one()
        assert row.dev_eui==NODE.dev_eui and row.last_seen==T0
        assert row.last_message_type is None and row.explicit_fire_active is None
        assert c.scalar(select(func.count()).select_from(control_uplinks))==0
    db.dispose()
