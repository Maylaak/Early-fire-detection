"""Ingestion and storage.

Runs against a temporary SQLite file. Set TEST_DATABASE_URL to a Postgres URL
(e.g. the docker-compose one) to run the same tests against Postgres too.
WARNING: that drops and recreates the tables in the given database.
"""

import base64
import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import DBAPIError

from gateway.chirpstack import EnvelopeError, build_uplink, parse_uplink, uplink_topic
from gateway.codec import encode
from gateway.config import DEFAULT_CONFIG_PATH, load_config
from gateway.db import gateways, init_schema, make_engine, nodes, readings
from gateway.ingest import Ingestor
from gateway.sim.model import NodeModel
from gateway.store import Store

CFG = load_config(DEFAULT_CONFIG_PATH, env={})
SIM = CFG.simulator
NODE = SIM.nodes[1]
T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
TOPIC = uplink_topic(SIM.application_id, NODE.dev_eui)


def make_envelope(f_cnt=0, t=T0, **overrides):
    s = NodeModel(NODE, SIM, random.Random(1), T0).sample(t)
    env = build_uplink(
        application_id=SIM.application_id, application_name=SIM.application_name,
        device_name=NODE.name, dev_eui=NODE.dev_eui, dev_addr=NODE.dev_addr,
        f_cnt=f_cnt, f_port=CFG.lorawan.heartbeat_f_port, payload=encode(s.reading),
        rssi=s.rssi, snr=s.snr, time=t, gateway_id=SIM.gateway_id,
    )
    env.update(overrides)
    return env


def body(env) -> bytes:
    return json.dumps(env).encode()


# --- envelope parsing ------------------------------------------------------

def test_parse_simulator_envelope():
    env = make_envelope(f_cnt=42)
    up = parse_uplink(TOPIC, body(env), received_at=T0 + timedelta(seconds=1))
    assert up.dev_eui == NODE.dev_eui
    assert up.device_name == NODE.name
    assert up.application_id == SIM.application_id
    assert up.f_cnt == 42 and up.f_port == CFG.lorawan.heartbeat_f_port
    assert up.time == T0
    assert up.rssi == env["rxInfo"][0]["rssi"]
    assert up.gateway_count == 1
    assert up.deduplication_id == env["deduplicationId"]
    assert len(up.data) == 11


@pytest.mark.parametrize("port", [CFG.lorawan.heartbeat_f_port, CFG.lorawan.alarm_mode_f_port])
def test_port_is_stored_with_the_reading(engine, port):
    Ingestor(Store(engine)).handle(TOPIC, body(make_envelope(fPort=port)), T0)
    with engine.connect() as conn:
        assert conn.execute(select(readings.c.f_port)).scalar_one() == port


def test_old_database_gets_the_new_column_and_keeps_its_data(tmp_path):
    from sqlalchemy import inspect, text
    eng = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    init_schema(eng)
    Ingestor(Store(eng)).handle(TOPIC, body(make_envelope()), T0)
    with eng.begin() as conn:  # what a database from before this column looks like
        conn.execute(text("ALTER TABLE readings DROP COLUMN f_port"))
    assert "f_port" not in {c["name"] for c in inspect(eng).get_columns("readings")}

    init_schema(eng)  # the detection service does this at start-up
    with eng.connect() as conn:
        rows = conn.execute(select(readings.c.f_cnt, readings.c.f_port)).all()
    assert rows == [(0, None)]  # the old reading survives; its port is unknown
    Ingestor(Store(eng)).handle(TOPIC, body(make_envelope(f_cnt=1, t=T0 + timedelta(minutes=10))), T0)
    with eng.connect() as conn:
        assert conn.execute(select(readings.c.f_port).where(readings.c.f_cnt == 1)).scalar_one() \
            == CFG.lorawan.heartbeat_f_port
    eng.dispose()


def test_missing_default_fields_are_tolerated():
    env = make_envelope()
    del env["fCnt"], env["time"], env["deduplicationId"]
    received = T0 + timedelta(seconds=5)
    up = parse_uplink(TOPIC, body(env), received)
    assert up.f_cnt == 0
    assert up.time == received
    assert up.deduplication_id is None


def test_best_gateway_wins():
    env = make_envelope()
    env["rxInfo"] = [
        {"gatewayId": "a", "rssi": -118, "snr": -9.0},
        {"gatewayId": "b", "rssi": -95, "snr": 6.5},
        {"gatewayId": "c", "rssi": -110, "snr": -2.0},
    ]
    up = parse_uplink(TOPIC, body(env), T0)
    assert (up.rssi, up.snr, up.gateway_count) == (-95, 6.5, 3)


def test_zulu_and_nanosecond_timestamps():
    env = make_envelope(time="2026-07-01T12:00:00.123456789Z")
    up = parse_uplink(TOPIC, body(env), T0)
    assert up.time == datetime(2026, 7, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "topic,raw",
    [
        ("application/x/device/70b3d57ed0000002/event/join", None),  # not an uplink
        (TOPIC, b"not json"),
        (TOPIC, b"[1, 2]"),
        (TOPIC, body(make_envelope(data=None))),
        (TOPIC, body(make_envelope(data="***"))),
        (uplink_topic(SIM.application_id, "70b3d57ed0000099"), None),  # devEui mismatch
    ],
)
def test_bad_envelopes_raise(topic, raw):
    with pytest.raises(EnvelopeError):
        parse_uplink(topic, raw if raw is not None else body(make_envelope()), T0)


# --- storage (the `engine` fixture is in conftest.py) -----------------------

def test_store_and_read_back(engine):
    ing = Ingestor(Store(engine))
    result = ing.handle(TOPIC, body(make_envelope(f_cnt=7)), T0 + timedelta(seconds=1))
    assert result is not None

    with engine.connect() as conn:
        row = conn.execute(select(readings)).one()
        node = conn.execute(select(nodes)).one()
    assert row.f_cnt == 7
    assert row.time == T0 and row.time.tzinfo is not None
    assert row.gas_resistance_ohm == pytest.approx(result.reading.gas_resistance_ohm)
    assert node.name == NODE.name and node.state == "WARMUP" and node.online
    assert node.last_seen == T0


def test_redelivered_uplink_is_stored_once(engine):
    ing = Ingestor(Store(engine))
    raw = body(make_envelope())
    assert ing.handle(TOPIC, raw, T0) is not None
    assert ing.handle(TOPIC, raw, T0) is None
    assert ing.stats.duplicates == 1
    with engine.connect() as conn:
        assert len(conn.execute(select(readings)).all()) == 1


def test_late_uplink_does_not_move_last_seen_backwards(engine):
    ing = Ingestor(Store(engine))
    ing.handle(TOPIC, body(make_envelope(f_cnt=2, t=T0 + timedelta(minutes=2))), T0)
    ing.handle(TOPIC, body(make_envelope(f_cnt=1, t=T0 + timedelta(minutes=1))), T0)
    with engine.connect() as conn:
        node = conn.execute(select(nodes)).one()
        assert len(conn.execute(select(readings)).all()) == 2
    assert node.last_seen == T0 + timedelta(minutes=2)
    assert node.last_f_cnt == 2


def test_bad_messages_are_skipped_not_raised(engine):
    ing = Ingestor(Store(engine))
    short = make_envelope(data=base64.b64encode(b"\x01\x02").decode())
    assert ing.handle(TOPIC, b"garbage", T0) is None
    assert ing.handle(TOPIC, body(short), T0) is None  # codec rejects 2-byte payload
    assert ing.stats.rejected == 2
    assert ing.handle(TOPIC, body(make_envelope()), T0) is not None


def test_sqlite_read_only_engine_cannot_write(tmp_path):
    url = f"sqlite:///{tmp_path / 'ro.db'}"
    writer = make_engine(url)
    init_schema(writer)
    Ingestor(Store(writer)).handle(TOPIC, body(make_envelope()), T0)

    reader = make_engine(url, read_only=True)
    with reader.connect() as conn:
        assert len(conn.execute(select(readings)).all()) == 1
    with pytest.raises(DBAPIError, match="readonly"):
        with reader.begin() as conn:
            conn.execute(insert(nodes).values(
                dev_eui="ffffffffffffffff", name="x", first_seen=T0, last_seen=T0))
    reader.dispose()
    writer.dispose()


def test_sqlite_read_only_engine_does_not_create_missing_file(tmp_path):
    reader = make_engine(f"sqlite:///{tmp_path / 'missing.db'}", read_only=True)
    with pytest.raises(DBAPIError):
        with reader.connect() as conn:
            conn.execute(select(nodes)).all()
    assert not (tmp_path / "missing.db").exists()


# --- gateways ------------------------------------------------------------------

def test_parse_lists_each_receiving_gateway():
    env = make_envelope()
    second = dict(env["rxInfo"][0], gatewayId="0016C001FF100002", rssi=-120, snr=-9.5)
    env["rxInfo"].append(second)
    up = parse_uplink(TOPIC, body(env), T0)
    assert [g.gateway_id for g in up.gateways] == [SIM.gateway_id, "0016c001ff100002"]
    assert up.gateways[1].rssi == -120 and up.gateways[1].snr == -9.5


def test_ingest_records_gateways(engine):
    ing = Ingestor(Store(engine))
    ing.handle(TOPIC, body(make_envelope(f_cnt=1, t=T0 + timedelta(minutes=10))), T0)
    ing.handle(TOPIC, body(make_envelope(f_cnt=0, t=T0)), T0)  # late: must not move last_seen back
    with engine.connect() as conn:
        gw = conn.execute(select(gateways)).one()
    assert gw.gateway_id == SIM.gateway_id
    assert gw.uplinks == 2
    assert gw.first_seen == T0 + timedelta(minutes=10)
    assert gw.last_seen == T0 + timedelta(minutes=10)