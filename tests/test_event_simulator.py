import base64
import json
import random
from datetime import timedelta

import pytest
from gateway.config import load_config
from gateway.codec import SensorReading, decode_message
from gateway.sim.model import Sample, NodeModel
from gateway.sim.event_firmware import EventNodeFirmware
from gateway.sim.__main__ import _envelope
from gateway.ingest import Ingestor
from gateway.chirpstack import uplink_topic
from gateway.detect import DetectionEngine
from gateway.store import Store
from tests.test_simulator import T0
from tests.test_service import make_pipeline, close

class QuietModel:
    def __init__(self,cfg):
        self.node=cfg.simulator.nodes[0];self.spreading_factor=12
        self.calls=0;self.fire=False
    def sample(self,t):
        self.calls+=1
        return Sample(t,self.calls,SensorReading(28 if self.fire else 20,35 if self.fire else 50,
                      1000,50000 if self.fire else 200000,3.6),-90,7,0,False)

@pytest.mark.parametrize('speed',[1,60])
def test_daily_schedule_and_local_sampling(speed):
    cfg=load_config(env={'DEMO_SPEEDUP':str(speed)})
    model=QuietModel(cfg); fw=EventNodeFirmware(model,cfg)
    txs=[]
    for i in range(2881):
        tx=fw.step(T0+i*fw.interval)
        if tx: txs.append(tx)
    assert model.calls==2881
    assert [tx.reason for tx in txs]==['HEARTBEAT']*3
    assert [tx.sample.f_cnt for tx in txs]==[0,1,2]
    assert [tx.sample.time for tx in txs]==[T0+timedelta(seconds=i*86400/speed) for i in range(3)]


def test_restart_reschedule_and_duplicate_ticks(tmp_path):
    cfg=load_config(env={});path=tmp_path/'sim.sqlite'
    fw=EventNodeFirmware(QuietModel(cfg),cfg,path)
    assert fw.step(T0).sample.f_cnt==0
    restart=EventNodeFirmware(QuietModel(cfg),cfg,path)
    assert restart.step(T0) is None
    assert restart.step(T0+timedelta(hours=12)) is None
    assert restart.step(T0+timedelta(days=1)).sample.f_cnt==1
    assert restart.step(T0+timedelta(days=1)) is None
    fast=load_config(env={'DEMO_SPEEDUP':'60'})
    changed=EventNodeFirmware(QuietModel(fast),fast,path)
    t=T0+timedelta(days=1,seconds=1)
    assert changed.step(t) is None
    assert changed.step(t+timedelta(minutes=24)).sample.f_cnt==2


def test_fire_is_prompt_bounded_and_duty_cycle_limited():
    cfg=load_config(env={'DEMO_SPEEDUP':'60'});m=QuietModel(cfg);fw=EventNodeFirmware(m,cfg)
    sent=[]
    for i in range(60):
        if i==35:m.fire=True
        tx=fw.step(T0+i*fw.interval)
        if tx:sent.append(tx)
    assert [tx.reason for tx in sent]==['HEARTBEAT','FIRE_ALERT']
    assert sent[1].sample.time < T0+timedelta(seconds=45)
    assert sent[1].sample.time-sent[0].sample.time>=fw.min_gap
    # A queued fire survives radio restriction and sends at the first legal sample.
    fw.state['pending']=True
    fw.state['last_tx']=T0+timedelta(seconds=60)
    assert fw.step(T0+timedelta(seconds=60)) is None
    tx=fw.step(T0+timedelta(seconds=60)+fw.min_gap+fw.interval)
    assert tx.reason=='FIRE_ALERT'


def test_injected_fire_through_backend(engine):
    from sqlalchemy import select, func
    from gateway.db import readings, events
    from tests.test_control_messages import Recorder
    cfg=load_config(env={'DEMO_SPEEDUP':'60'});n=cfg.simulator.nodes[2]
    model=NodeModel(n,cfg.simulator,random.Random(5),T0)
    fw=EventNodeFirmware(model,cfg);det=DetectionEngine(cfg.detection)
    recorder = Recorder()
    p=make_pipeline(Store(engine),det,[recorder]);ing=Ingestor();kinds=[]
    try:
        for i in range(100):
            t=T0+i*fw.interval
            if i==35:model.inject_smoke(t)
            tx=fw.step(t)
            if tx:
                env=_envelope(cfg,model,tx.sample,tx.reason)
                kinds.append(decode_message(base64.b64decode(env['data'])).kind)
                got=ing.accept(uplink_topic(cfg.simulator.application_id,n.dev_eui),json.dumps(env).encode(),t)
                p.on_uplink(got,t)
                p.on_uplink(got,t)  # Re-delivery must not repeat persistence or notification.
        assert p.writer.flush(5)
    finally:close(p)
    assert kinds==['HEARTBEAT','FIRE_ALERT']
    assert len(recorder.sent) == 1
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(readings)) == 0
        assert conn.scalar(select(func.count()).select_from(events)) == 1
    assert det.nodes[n.dev_eui].explicit_fire_active
    assert not det.check_offline(T0+timedelta(minutes=60))
    assert det.check_offline(T0+timedelta(minutes=75))
    assert det.nodes[n.dev_eui].state=='ALARM'


def test_outbox_restart_ack_and_saved_detector(tmp_path):
    cfg=load_config(env={'DEMO_SPEEDUP':'60'});path=tmp_path/'outbox.sqlite'
    fw=EventNodeFirmware(QuietModel(cfg),cfg,path)
    tx=fw.step(T0)
    for i in range(1,35):fw.step(T0+i*fw.interval)
    restarted=EventNodeFirmware(QuietModel(cfg),cfg,path)
    assert restarted.pending_transmissions()==[tx]
    assert restarted.step(T0+35*fw.interval) is None
    assert restarted.detector.samples>=30
    restarted.acknowledge(tx)
    assert restarted.pending_transmissions()==[]
    again=EventNodeFirmware(QuietModel(cfg),cfg,path)
    assert again.pending_transmissions()==[]
    assert again.step(T0+36*fw.interval) is None


def test_cli_dry_run_nine_control_heartbeats(capsys):
    from argparse import Namespace
    from gateway.sim.__main__ import cmd_run
    cfg=load_config(env={'DEMO_SPEEDUP':'60'})
    args=Namespace(interval=None,seed=42,dry_run=True,count=9)
    assert cmd_run(cfg,args)==0
    envelopes=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(envelopes)==9
    assert len({e['deviceInfo']['devEui'] for e in envelopes})==9
    assert all(decode_message(base64.b64decode(e['data'])).kind=='HEARTBEAT' for e in envelopes)
    assert all(e['fCnt']==0 for e in envelopes)


@pytest.mark.parametrize('speed', [1, 60])
def test_nine_nodes_two_quiet_days(speed):
    cfg = load_config(env={'DEMO_SPEEDUP': str(speed)})
    firmwares = []
    for node in cfg.simulator.nodes:
        model = QuietModel(cfg)
        model.node = node
        firmwares.append(EventNodeFirmware(model, cfg))
    sent = []
    # Half-open days include the boot heartbeat, exclude the next day's boundary.
    for tick in range(2880):
        for fw in firmwares:
            tx = fw.step(T0 + tick * fw.interval)
            if tx:
                sent.append((fw.model.node.dev_eui, tx))
                env = _envelope(cfg, fw.model, tx.sample, tx.reason)
                assert base64.b64decode(env['data']) == b'\x02\x01'
    day = timedelta(seconds=86400 / speed)
    for index in range(2):
        messages = [(node, tx) for node, tx in sent
                    if T0 + index * day <= tx.sample.time < T0 + (index + 1) * day]
        assert len(messages) == 9
        assert len({node for node, _ in messages}) == 9
        assert all(tx.sample.f_cnt == index for _, tx in messages)
    assert all(fw.model.calls == 2880 for fw in firmwares)


def test_connectivity_boundary_recovery_and_dashboard(engine):
    from fastapi.testclient import TestClient
    from gateway.web.app import create_app
    from tests.test_control_messages import accepted
    cfg = load_config(env={'DEMO_SPEEDUP': '60'})
    det = DetectionEngine(cfg.detection)
    p = make_pipeline(Store(engine), det)
    ing = Ingestor()
    try:
        p.on_uplink(accepted(ing, 'FIRE_ALERT', t=T0), T0)
        expiry = T0 + timedelta(minutes=cfg.detection.explicit_missing_minutes)
        assert det.check_offline(expiry) == []
        assert len(det.check_offline(expiry + timedelta(seconds=1))) == 1
        t = expiry + timedelta(seconds=2)
        p.on_uplink(accepted(ing, 'HEARTBEAT', 2, t), t)
        assert p.writer.flush(5)
        d = next(iter(det.nodes.values()))
        assert d.online and d.explicit_fire_active and d.state == 'ALARM'
        with TestClient(create_app(cfg, engine, clock=lambda: t)) as client:
            config = client.get('/api/console/config').json()
            assert config['reporting_mode'] == 'event_driven'
            assert config['explicit_heartbeat_minutes'] == 24
            assert config['explicit_missing_minutes'] == 72
            status = client.get('/api/console/status').json()
            assert status['pill'] == 'FIRE_ALARM'
            assert not status['nodes'][0]['missing']
            assert status['nodes'][0]['last_message_type'] == 'HEARTBEAT'
            assert len(status['never_seen']) == 8
    finally:
        close(p)


def test_event_mode_summary_and_transmission_log(caplog):
    import logging
    from gateway.config import summary
    from gateway.sim.__main__ import _log_sample
    cfg = load_config(env={'DEMO_SPEEDUP': '60'})
    text = summary(cfg)
    assert 'every 24 min (event-driven)' in text
    assert '72 min' in text
    assert 'warm-up 30 samples' in text
    model = QuietModel(cfg)
    tx = EventNodeFirmware(model, cfg).step(T0)
    with caplog.at_level(logging.INFO):
        _log_sample(model, tx)
    assert 'HEARTBEAT payload=2 bytes' in caplog.text
    assert 'gas=' not in caplog.text
