"""Web app: read-only API and pages."""

import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.chirpstack import build_uplink, uplink_topic
from gateway.codec import encode
from gateway.config import DEFAULT_CONFIG_PATH, load_config
from gateway.db import init_schema, make_engine
from gateway.detect import DetectionEngine
from gateway.ingest import Ingestor
from gateway.sim.model import NodeModel
from gateway.store import Store
from gateway.web import app as web_app
from gateway.web.app import create_app

CFG = load_config(DEFAULT_CONFIG_PATH, env={})
SIM = CFG.simulator
START = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
SMOKE_AT = START + timedelta(minutes=60)
END = START + timedelta(minutes=80)  # node-3 is in ALARM by now
STEP = timedelta(seconds=60)


def populate(engine, minutes=80):
    """Two nodes through the real ingest + detection path; smoke on node-3."""
    store, det, ing = Store(engine), DetectionEngine(CFG.detection), Ingestor(Store(engine))
    for node in SIM.nodes[1:3]:
        model = NodeModel(node, SIM, random.Random(5), START)
        if node.name == "node-3":
            model.inject_smoke(SMOKE_AT)
        for i in range(minutes):
            t = START + i * STEP
            s = model.sample(t)
            env = build_uplink(
                application_id=SIM.application_id, application_name=SIM.application_name,
                device_name=node.name, dev_eui=node.dev_eui, dev_addr=node.dev_addr,
                f_cnt=s.f_cnt, f_port=CFG.lorawan.heartbeat_f_port, payload=encode(s.reading),
                rssi=s.rssi, snr=s.snr, time=t, gateway_id=SIM.gateway_id)
            got = ing.handle(uplink_topic(SIM.application_id, node.dev_eui), json.dumps(env).encode(), t)
            ev = det.process(node.dev_eui, node.name, t, got.reading)
            store.save_evaluation(got.reading_id, ev, det.nodes[node.dev_eui])


@pytest.fixture
def client(engine):
    populate(engine)
    return TestClient(create_app(CFG, engine, clock=lambda: END))


def test_pages_are_served(client):
    for path in ["/", "/node/70b3d57ed0000003", "/alarms", "/static/charts.js", "/static/style.css"]:
        r = client.get(path)
        assert r.status_code == 200, path
    assert "Wildfire gateway" in client.get("/").text


def test_status_lists_nodes_and_raises_banner(client):
    data = client.get("/api/status").json()
    by_name = {n["name"]: n for n in data["nodes"]}
    assert set(by_name) == {"node-2", "node-3"}
    assert by_name["node-3"]["state"] == "ALARM"
    assert by_name["node-2"]["state"] == "NORMAL"
    latest = by_name["node-3"]["latest"]
    assert latest["gas_vs_baseline_pct"] < -40  # smoke = gas resistance DROP
    assert by_name["node-3"]["rssi"] is not None and by_name["node-3"]["snr"] is not None
    s = data["summary"]
    assert s["alarm"] == ["node-3"] and s["watch"] == [] and s["offline"] == []
    assert s["stale"] is False


def test_stale_when_no_recent_data(engine):
    populate(engine, minutes=40)
    later = START + timedelta(hours=3)
    data = TestClient(create_app(CFG, engine, clock=lambda: later)).get("/api/status").json()
    assert data["summary"]["stale"] is True


def test_readings_for_chart(client):
    data = client.get("/api/nodes/70b3d57ed0000003/readings", params={"hours": 1}).json()
    pts = data["points"]
    assert len(pts) == 60  # one per minute for the last hour
    assert all(p["baseline_gas_ohm"] for p in pts)
    assert {"NORMAL", "WATCH", "ALARM"} <= {p["state"] for p in pts}
    assert data["node"]["name"] == "node-3"


def test_readings_validation(client):
    assert client.get("/api/nodes/ffffffffffffffff/readings").status_code == 404
    assert client.get("/api/nodes/70b3d57ed0000003/readings", params={"hours": 0}).status_code == 422
    assert client.get("/api/nodes/70b3d57ed0000003/readings", params={"hours": 99999}).status_code == 422


def test_readings_are_thinned_but_keep_anomalies(client, monkeypatch):
    monkeypatch.setattr(web_app, "MAX_POINTS", 20)
    data = client.get("/api/nodes/70b3d57ed0000003/readings", params={"hours": 2}).json()
    pts = data["points"]
    assert data["total_points"] == 80
    assert len(pts) < 80
    raw = client.get("/api/events", params={"kind": "alarms"}).json()  # sanity: there was an alarm
    assert raw["events"]
    anomalous = [p for p in pts if p["state"] in ("WATCH", "ALARM")]
    assert len(anomalous) >= 10  # every WATCH/ALARM reading kept


def test_alarm_history(client):
    evs = client.get("/api/events", params={"kind": "alarms"}).json()["events"]
    assert [(e["node"], e["to_state"]) for e in evs] == [("node-3", "ALARM"), ("node-3", "WATCH")]
    assert evs[0]["details"]["gas_drop_pct"] > 40
    everything = client.get("/api/events", params={"kind": "all"}).json()["events"]
    assert len(everything) == 4  # + two warm-up completions
    only_node2 = client.get("/api/events", params={"kind": "all", "dev_eui": "70b3d57ed0000002"}).json()
    assert {e["node"] for e in only_node2["events"]} == {"node-2"}


def test_web_app_has_no_write_endpoints():
    app = create_app(CFG, make_engine("sqlite://"))
    methods = {m for route in app.routes for m in (getattr(route, "methods", None) or set())}
    assert methods <= {"GET", "HEAD"}, methods


def test_missing_database_gives_503(tmp_path):
    db = tmp_path / "never-created.db"
    app = create_app(CFG, make_engine(f"sqlite:///{db}", read_only=True))
    r = TestClient(app).get("/api/status")
    assert r.status_code == 503
    assert "detection service" in r.json()["detail"]
    assert not db.exists()  # the web app must not create the database


def test_read_only_engine_is_used_by_default(tmp_path):
    url = f"sqlite:///{tmp_path / 'w.db'}"
    writer = make_engine(url)
    init_schema(writer)
    populate(writer, minutes=5)
    cfg = CFG.model_copy(update={"database": CFG.database.model_copy(update={"web_url": url})})
    client = TestClient(create_app(cfg, clock=lambda: START + timedelta(minutes=5)))
    assert len(client.get("/api/status").json()["nodes"]) == 2
