"""Operator console event log entries (pure logic) and their storage."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from gateway import console_log as clog
from gateway.config import DEFAULT_CONFIG_PATH, load_config
from gateway.db import console_log
from gateway.detect import ALARM, NORMAL, WATCH, Event, NodeDetector
from gateway.store import Store

CFG = load_config(DEFAULT_CONFIG_PATH, env={})
SITES = CFG.sites
T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
# Shipped registry: F-07 has 3 nodes (...01-03), F-08 has 1 (...04).
EUI = {s.label: s.dev_eui for s in SITES.nodes}


def detectors(**states):
    """{dev_eui: NodeDetector} with the given label=STATE; everything else NORMAL."""
    out = {}
    for s in SITES.nodes:
        d = NodeDetector(s.dev_eui, s.label, CFG.detection)
        d.state = states.get(s.label.replace("-", "_"), NORMAL)
        out[s.dev_eui] = d
    return out


def event(label, to_state, kind="STATE"):
    return Event(EUI[label], label, T0, kind, f"{label}: something", notify=True, to_state=to_state)


def test_node_entry_levels_and_block():
    entries = clog.node_entries(
        [event("F-07-03", ALARM), event("F-07-01", WATCH), event("F-08-01", NORMAL),
         event("F-07-02", None, kind="OFFLINE")], SITES)
    assert [e.level for e in entries] == ["alarm", "warning", "info", "warning"]
    assert entries[0].block == "F-07" and entries[2].block == "F-08"
    assert entries[0].dev_eui == EUI["F-07-03"]


def test_block_tracker_story():
    tr = clog.BlockTracker(SITES)
    tr.prime(detectors())
    t = T0

    def step(**states):
        nonlocal t
        t += timedelta(minutes=1)
        return [e.message for e in tr.update(detectors(**states), t)]

    assert step() == []
    assert step(F_07_02=WATCH) == ["Block F-07 on WATCH: F-07-02 (1 of 3 nodes)"]
    assert step(F_07_02=WATCH) == []  # no repeat while nothing changes
    assert step(F_07_02=ALARM) == ["BLOCK F-07 FIRE ALARM: F-07-02 (1 of 3 nodes)"]
    assert step(F_07_02=ALARM, F_07_03=ALARM) == [
        "Block F-07 alarm spreading: F-07-02, F-07-03 (2 of 3 nodes)"]
    assert step(F_07_02=ALARM) == []  # shrinking alarm is not news; clearing is
    assert step(F_07_02=WATCH) == []  # ALARM -> WATCH: still not clear
    assert step() == ["Block F-07 all clear"]


def test_blocks_are_independent():
    tr = clog.BlockTracker(SITES)
    tr.prime(detectors())
    msgs = [e.message for e in tr.update(detectors(F_08_01=ALARM), T0)]
    assert msgs == ["BLOCK F-08 FIRE ALARM: F-08-01 (1 of 1 nodes)"]


def test_primed_alarm_is_not_reannounced_after_restart():
    tr = clog.BlockTracker(SITES)
    tr.prime(detectors(F_07_01=ALARM))
    assert tr.update(detectors(F_07_01=ALARM), T0) == []


def test_system_check():
    ok = clog.system_check(T0, detectors(), 1, 1)
    assert ok.level == "info"
    assert ok.message == "System check: 4/4 nodes reporting, 1/1 gateways online, 0 in alarm, 0 on watch"
    ds = detectors(F_07_01=ALARM)
    ds[EUI["F-08-01"]].online = False
    bad = clog.system_check(T0, ds, 0, 1)
    assert bad.level == "warning"
    assert "3/4 nodes reporting, 0/1 gateways online, 1 in alarm" in bad.message


def test_notification_entries_use_labels_only():
    sent = clog.notification(T0, "FIRE ALARM", "F-07-03", "telegram", True, dev_eui=EUI["F-07-03"])
    failed = clog.notification(T0, "FIRE ALARM", "F-07-03", "telegram", False, "timeout")
    assert sent.message == 'telegram: sent "FIRE ALARM" for F-07-03'
    assert failed.level == "warning" and failed.message.endswith("(timeout)")
    assert EUI["F-07-03"] not in sent.message


def test_entries_are_stored_in_order(engine):
    store = Store(engine)
    store.append_log([clog.service_started(T0, 4, ["console"], 1),
                      clog.system_check(T0 + timedelta(minutes=60), detectors(), 1, 1)])
    store.append_log([])
    with engine.connect() as conn:
        rows = conn.execute(select(console_log).order_by(console_log.c.id)).all()
    assert [r.category for r in rows] == ["system", "system"]
    assert rows[0].message.startswith("Detection service started: 4 nodes known")
    assert rows[1].time == T0 + timedelta(minutes=60)
