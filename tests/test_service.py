"""Detection wired to storage: results persisted, restart replay rebuilds state."""

import json
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from gateway.chirpstack import build_uplink, uplink_topic
from gateway.codec import encode
from gateway.config import DEFAULT_CONFIG_PATH, load_config
from gateway import console_log as clog
from gateway.db import console_log, events, nodes, readings
from gateway.detect import ALARM, NORMAL, WATCH, DetectionEngine
from gateway.ingest import Ingestor
from gateway.service import _label, _log_events, _replay
from gateway.sim.model import NodeModel
from gateway.store import Store

CFG = load_config(DEFAULT_CONFIG_PATH, env={})
SIM = CFG.simulator
NODE = SIM.nodes[2]
STEP = timedelta(seconds=60)
# Fixed start: the simulator's day/night cycle and weather depend on the time,
# so "now" would make these tests depend on when they are run.
START = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
SMOKE_AT = START + timedelta(minutes=60)
END = START + timedelta(minutes=90)


LABEL = "F-07-03"  # NODE's site label in the shipped config


def run_pipeline(store, engine, start, minutes, smoke_at=None):
    """What the service loop does for each message, minus MQTT and notifiers."""
    tracker = clog.BlockTracker(CFG.sites)
    model = NodeModel(NODE, SIM, random.Random(5), start)
    if smoke_at is not None:
        model.inject_smoke(smoke_at)
    ingestor = Ingestor(store)
    topic = uplink_topic(SIM.application_id, NODE.dev_eui)
    for i in range(minutes):
        t = start + i * STEP
        s = model.sample(t)
        env = build_uplink(
            application_id=SIM.application_id, application_name=SIM.application_name,
            device_name=NODE.name, dev_eui=NODE.dev_eui, dev_addr=NODE.dev_addr,
            f_cnt=s.f_cnt, f_port=CFG.lorawan.heartbeat_f_port, payload=encode(s.reading),
            rssi=s.rssi, snr=s.snr, time=t, gateway_id=SIM.gateway_id)
        ingested = ingestor.handle(topic, json.dumps(env).encode(), t)
        ev = engine.process(NODE.dev_eui, _label(CFG, NODE.dev_eui, NODE.name), t, ingested.reading)
        store.save_evaluation(ingested.reading_id, ev, engine.nodes[NODE.dev_eui])
        _log_events(store, CFG, tracker, engine, ev.events, t)


def test_detection_results_are_persisted(engine):
    store, det = Store(engine), DetectionEngine(CFG.detection)
    run_pipeline(store, det, START, 90, smoke_at=SMOKE_AT)

    with engine.connect() as conn:
        node = conn.execute(select(nodes)).one()
        evs = conn.execute(select(events).order_by(events.c.id)).all()
        # A reading in the middle of the smoke's full-strength phase.
        mid = conn.execute(select(readings).where(
            readings.c.time == SMOKE_AT + timedelta(minutes=15))).one()

    assert node.state == ALARM and node.online
    assert [e.to_state for e in evs] == [NORMAL, WATCH, ALARM]
    assert evs[-1].details["gas_drop_pct"] > 40
    # Operators' names in messages; the devEUI stays in its own column.
    assert evs[-1].message.startswith(f"{LABEL}: FIRE ALARM")
    assert NODE.dev_eui not in evs[-1].message and evs[-1].dev_eui == NODE.dev_eui
    assert node.name == NODE.name  # ChirpStack device name kept in the nodes table
    assert mid.state == ALARM
    # The baseline is frozen at pre-smoke values, well above the smoky reading.
    assert mid.baseline_gas_ohm > 1.5 * mid.gas_resistance_ohm


def test_restart_replay_rebuilds_state_without_new_events(engine):
    store = Store(engine)
    run_pipeline(store, DetectionEngine(CFG.detection), START, 90, smoke_at=SMOKE_AT)
    with engine.connect() as conn:
        n_events = len(conn.execute(select(events)).all())

    fresh = DetectionEngine(CFG.detection)  # as if the service restarted
    _replay(CFG, store, fresh, now=END)

    d = fresh.nodes[NODE.dev_eui]
    assert d.state == ALARM
    assert d.name == LABEL
    assert d.samples >= CFG.detection.warmup_samples
    with engine.connect() as conn:
        assert len(conn.execute(select(events)).all()) == n_events  # nothing re-raised
        assert conn.execute(select(nodes.c.state)).scalar_one() == ALARM


def test_offline_event_is_saved(engine):
    store, det = Store(engine), DetectionEngine(CFG.detection)
    run_pipeline(store, det, START, 40)
    offline = det.check_offline(START + timedelta(hours=2))
    assert len(offline) == 1
    ids = store.save_events(offline, det.nodes)
    store.mark_notified(ids)
    with engine.connect() as conn:
        assert conn.execute(select(nodes.c.online)).scalar_one() is False
        ev = conn.execute(select(events).where(events.c.kind == "OFFLINE")).one()
    assert ev.notified


def test_console_log_tells_the_story(engine):
    store, det = Store(engine), DetectionEngine(CFG.detection)
    run_pipeline(store, det, START, 90, smoke_at=SMOKE_AT)
    with engine.connect() as conn:
        rows = conn.execute(select(console_log).order_by(console_log.c.time, console_log.c.id)).all()
    msgs = [r.message for r in rows]
    assert msgs[0].startswith(f"{LABEL}: warm-up complete, monitoring")
    assert any(m.startswith(f"{LABEL}: WATCH") for m in msgs)
    assert "Block F-07 on WATCH: F-07-03 (1 of 3 nodes)" in msgs
    assert "BLOCK F-07 FIRE ALARM: F-07-03 (1 of 3 nodes)" in msgs
    assert {r.level for r in rows if "FIRE ALARM" in r.message} == {"alarm"}
    assert all(NODE.dev_eui not in m for m in msgs)