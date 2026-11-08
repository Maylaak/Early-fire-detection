"""Detection wired to storage: results persisted, restart replay rebuilds state."""

import json
import os
import queue
import random
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from gateway import mqtt as mqtt_mod
from gateway import service as service_mod
from gateway.mqtt import BrokerUnavailable
from gateway.notify import Notifier
from gateway.writer import Job

from gateway.chirpstack import build_uplink, uplink_topic
from gateway.codec import encode
from gateway.config import DEFAULT_CONFIG_PATH, load_config
from gateway import console_log as clog
from gateway.db import console_log, events, nodes, readings, utcnow
from gateway.detect import ALARM, NORMAL, WATCH, DetectionEngine
from gateway.ingest import Ingestor
from gateway.notify import Dispatcher, headline
from gateway.service import Pipeline, _replay
from gateway.sim.model import NodeModel
from gateway.store import Store
from gateway.writer import LogJob, NotifiedJob, Writer

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


TOPIC = uplink_topic(SIM.application_id, NODE.dev_eui)


def make_pipeline(store, engine, notifiers=()):
    """The service's Pipeline with a real writer and dispatcher, minus MQTT."""
    writer = Writer(store, CFG.database.max_queued_writes, retry_seconds=0.02).start()

    def on_result(e, _ref, channel, ok, detail):  # as in service.run()
        writer.submit(LogJob([clog.notification(utcnow(), headline(e), e.node_name, channel, ok,
                                                detail, e.dev_eui)]))

    dispatcher = Dispatcher(list(notifiers), on_delivered=lambda ref: writer.submit(NotifiedJob(ref)),
                            on_result=on_result, retry_delays=(0,)).start()
    return Pipeline(CFG, engine, clog.BlockTracker(CFG.sites), writer, dispatcher)


def close(p):
    p.dispatcher.stop()
    p.writer.stop(5)


def uplink_body(s, t):
    return json.dumps(build_uplink(
        application_id=SIM.application_id, application_name=SIM.application_name,
        device_name=NODE.name, dev_eui=NODE.dev_eui, dev_addr=NODE.dev_addr,
        f_cnt=s.f_cnt, f_port=CFG.lorawan.heartbeat_f_port, payload=encode(s.reading),
        rssi=s.rssi, snr=s.snr, time=t, gateway_id=SIM.gateway_id)).encode()


def feed(p, ingestor, model, start, minutes):
    """One uplink per STEP through Ingestor.accept and Pipeline.on_uplink, as
    the service loop does. Returns the message bodies sent."""
    bodies = []
    for i in range(minutes):
        t = start + i * STEP
        bodies.append(uplink_body(model.sample(t), t))
        got = ingestor.accept(TOPIC, bodies[-1], t)
        if got is not None:
            p.on_uplink(got, t)
    return bodies


def run_pipeline(store, engine, start, minutes, smoke_at=None):
    """What the service loop does for each message, minus MQTT and notifiers."""
    model = NodeModel(NODE, SIM, random.Random(5), start)
    if smoke_at is not None:
        model.inject_smoke(smoke_at)
    p = make_pipeline(store, engine)
    feed(p, Ingestor(), model, start, minutes)
    assert p.writer.flush(10)
    close(p)


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


# --- duplicates (MQTT QoS 1 delivers at least once) --------------------------------


def test_a_redelivered_uplink_is_stored_once_and_counted_once(engine):
    store, det = Store(engine), DetectionEngine(CFG.detection)
    p, ing = make_pipeline(store, det), Ingestor()
    model = NodeModel(NODE, SIM, random.Random(5), START)
    for i in range(5):
        t = START + i * STEP
        body = uplink_body(model.sample(t), t)
        for _ in range(3):  # the broker delivers each message three times
            got = ing.accept(TOPIC, body, t)
            if got is not None:
                p.on_uplink(got, t)
    assert p.writer.flush(10)
    close(p)
    assert det.nodes[NODE.dev_eui].samples == 5  # each reading once in the baseline
    assert ing.stats.accepted == 5 and ing.stats.duplicates == 10
    with engine.connect() as conn:
        assert len(conn.execute(select(readings)).all()) == 5


def test_redelivery_after_a_restart_is_skipped(engine):
    # The service stored readings, then died before the broker saw its acks:
    # after the restart the broker delivers them again.
    store = Store(engine)
    model = NodeModel(NODE, SIM, random.Random(5), START)
    p = make_pipeline(store, DetectionEngine(CFG.detection))
    bodies = feed(p, Ingestor(), model, START, 40)
    assert p.writer.flush(10)
    close(p)

    fresh = DetectionEngine(CFG.detection)  # restart: empty memory, replay from the database
    _replay(CFG, store, fresh, now=START + 40 * STEP)
    samples = fresh.nodes[NODE.dev_eui].samples
    p, ing = make_pipeline(store, fresh), Ingestor()
    for i, body in enumerate(bodies[-10:]):  # the broker's queue, redelivered
        t = START + (30 + i) * STEP
        p.on_uplink(ing.accept(TOPIC, body, t), t)
    assert p.writer.flush(10)
    close(p)
    assert fresh.nodes[NODE.dev_eui].samples == samples  # not counted again
    with engine.connect() as conn:
        assert len(conn.execute(select(readings)).all()) == 40  # not stored again


# --- database outage: detection and alerts must not wait for Postgres ---------------


class FlakyStore(Store):
    """A Store whose database 'goes down': every write raises OperationalError,
    the way an unreachable Postgres does, until `down` is cleared."""

    down = False

    def _check(self):
        if self.down:
            raise OperationalError("INSERT ...", {}, ConnectionRefusedError("connection refused"))

    def save_reading(self, *a, **kw):
        self._check()
        return super().save_reading(*a, **kw)

    def save_events(self, *a, **kw):
        self._check()
        return super().save_events(*a, **kw)

    def mark_notified(self, *a, **kw):
        self._check()
        return super().mark_notified(*a, **kw)

    def append_log(self, *a, **kw):
        self._check()
        return super().append_log(*a, **kw)


class Recorder(Notifier):
    name = "recorder"

    def __init__(self):
        self.sent = []

    def send(self, event):
        self.sent.append(event)


def wait_for(cond, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_database_outage_does_not_stop_detection_or_alerts(engine):
    store, det, rec = FlakyStore(engine), DetectionEngine(CFG.detection), Recorder()
    p, ing = make_pipeline(store, det, [rec]), Ingestor()
    model = NodeModel(NODE, SIM, random.Random(5), START)
    model.inject_smoke(SMOKE_AT)
    feed(p, ing, model, START, 50)  # warm-up, database up
    assert p.writer.flush(10)

    store.down = True  # Postgres goes away just before the fire
    feed(p, ing, model, START + 50 * STEP, 40)
    wait_for(lambda: [e.to_state for e in rec.sent] == [WATCH, ALARM])  # alerts went out
    assert det.nodes[NODE.dev_eui].state == ALARM
    # A node falling silent is still noticed and alerted.
    p.check_offline(END + timedelta(hours=2))
    wait_for(lambda: [e.kind for e in rec.sent][-1] == "OFFLINE")
    p.system_check(END + timedelta(hours=2))
    wait_for(lambda: p.writer.database_down)
    assert p.writer.pending > 40  # everything is waiting, nothing lost
    with engine.connect() as conn:
        assert len(conn.execute(select(readings)).all()) == 50  # nothing written meanwhile

    store.down = False  # the database is back: the queue drains, in order
    assert p.writer.flush(10)
    close(p)
    assert not p.writer.database_down and p.writer.dropped == 0
    with engine.connect() as conn:
        rows = conn.execute(select(readings).order_by(readings.c.id)).all()
        evs = conn.execute(select(events).order_by(events.c.id)).all()
        log_rows = conn.execute(select(console_log).order_by(console_log.c.id)).all()
        node = conn.execute(select(nodes)).one()
    assert len(rows) == 90 and len({r.uplink_key for r in rows}) == 90
    assert [r.time for r in rows] == sorted(r.time for r in rows)
    assert [e.to_state or e.kind for e in evs] == [NORMAL, WATCH, ALARM, "OFFLINE"]
    assert all(e.notified for e in evs if e.kind == "OFFLINE" or e.to_state in (WATCH, ALARM))
    msgs = [r.message for r in log_rows]
    assert "BLOCK F-07 FIRE ALARM: F-07-03 (1 of 3 nodes)" in msgs
    assert f'recorder: sent "FIRE ALARM" for {LABEL}' in msgs
    assert any(m.startswith("System check:") for m in msgs)
    assert node.state == ALARM and node.online is False


class _Job(Job):
    def __init__(self, droppable):
        self.droppable = droppable


def test_full_write_queue_drops_oldest_readings_never_events():
    w = Writer(store=None, max_jobs=100)  # not started: nothing is written
    first_reading = _Job(True)
    w.submit(first_reading)
    events_jobs = [_Job(False) for _ in range(30)]
    for e in events_jobs:
        w.submit(e)
    readings_jobs = [_Job(True) for _ in range(120)]
    for r in readings_jobs:
        w.submit(r)
    assert w.pending == 100
    assert w.dropped == 51  # 1 + 120 readings, 30 events, room for 100
    queued = list(w._queue)
    assert all(e in queued for e in events_jobs)  # every event kept
    assert first_reading not in queued  # the oldest readings went first
    assert queued[-70:] == readings_jobs[-70:]  # the newest readings kept


def test_full_queue_of_events_only_still_keeps_them():
    w = Writer(store=None, max_jobs=100)
    for _ in range(150):
        w.submit(_Job(False))
    assert w.pending == 150 and w.dropped == 0


# --- MQTT session: the broker must queue for us while we are down ------------------


class FakeClient:
    made = []

    def __init__(self, api, client_id, clean_session):
        self.client_id, self.clean_session = client_id, clean_session
        FakeClient.made.append(self)

    def reconnect_delay_set(self, **kw):
        pass

    def connect(self, host, port, keepalive):
        pass

    def loop_start(self):
        pass


def test_service_connects_with_a_fixed_id_and_a_persistent_session(monkeypatch):
    monkeypatch.setattr(mqtt_mod.mqtt, "Client", FakeClient)
    FakeClient.made.clear()
    mqtt_mod.connect(CFG.mqtt, CFG.mqtt.service_client_id, [CFG.mqtt.uplink_topic], persistent=True)
    mqtt_mod.connect(CFG.mqtt, "wildfire-sim")
    service, sim = FakeClient.made
    assert service.client_id == CFG.mqtt.service_client_id == "wildfire-service"
    assert service.clean_session is False
    assert sim.client_id.startswith("wildfire-sim-") and sim.clean_session is True
    assert CFG.mqtt.qos == 1  # the broker only queues QoS 1/2 messages for a persistent session


def test_the_service_asks_for_the_persistent_session(monkeypatch, tmp_path):
    seen = {}

    def fake_connect(cfg, client_name, subscriptions=None, on_message=None, persistent=False):
        seen.update(client_name=client_name, persistent=persistent, subscriptions=subscriptions)
        raise BrokerUnavailable("stop here")

    monkeypatch.setattr(service_mod, "connect", fake_connect)
    cfg = CFG.model_copy(update={"database": CFG.database.model_copy(
        update={"url": f"sqlite:///{tmp_path / 'svc.db'}"})})
    assert service_mod.run(cfg) == 1
    assert seen == {"client_name": "wildfire-service", "persistent": True,
                    "subscriptions": [CFG.mqtt.uplink_topic]}


@pytest.mark.skipif(not os.environ.get("TEST_MQTT_HOST"),
                    reason="set TEST_MQTT_HOST (and TEST_MQTT_PORT) to a scratch Mosquitto")
def test_broker_queues_uplinks_while_the_service_is_down():
    """End to end against a real broker: messages published while the
    subscriber is away arrive when it reconnects."""
    mcfg = CFG.mqtt.model_copy(update={"host": os.environ["TEST_MQTT_HOST"],
                                       "port": int(os.environ.get("TEST_MQTT_PORT", "1883"))})
    cid, topic = f"test-svc-{os.getpid()}", f"application/test/device/{NODE.dev_eui}/event/up"
    got = queue.Queue()
    sub = mqtt_mod.connect(mcfg, cid, [topic], lambda m: got.put(m.payload), persistent=True)
    time.sleep(1)  # session and subscription exist
    sub.loop_stop()
    sub.disconnect()

    pub = mqtt_mod.connect(mcfg, "test-pub")
    for i in range(5):
        pub.publish(topic, f"m{i}", qos=1).wait_for_publish(5)
    pub.loop_stop()
    pub.disconnect()

    sub = mqtt_mod.connect(mcfg, cid, [topic], lambda m: got.put(m.payload), persistent=True)
    try:
        received = [got.get(timeout=10) for _ in range(5)]
    finally:
        sub.loop_stop()
        sub.disconnect()
    assert received == [f"m{i}".encode() for i in range(5)]