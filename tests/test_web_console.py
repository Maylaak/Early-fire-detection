"""Operator console API: labels not devEUIs, honest missing/NO DATA, zones, log."""

import json
import random
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gateway import console_log as clog
from gateway.chirpstack import build_uplink, uplink_topic
from gateway.codec import encode
from gateway.ingest import Ingestor
from gateway.sim.model import NodeModel
from gateway.store import Store
from gateway.web.app import create_app
from gateway.web.console import _alarm_zones, _block_shapes, _hull, _map_blocks, _unplaced
from tests.test_web import CFG, END, SIM, START, populate

NODE2, NODE3 = SIM.nodes[1], SIM.nodes[2]  # sites F-07-02 and F-07-03


def app_at(engine, now):
    return TestClient(create_app(CFG, engine, clock=lambda: now))


@pytest.fixture
def client(engine):
    populate(engine)  # node-2 normal, node-3 in ALARM at END
    return app_at(engine, END)


def test_console_config(client):
    c = client.get("/api/console/config").json()
    assert c["title"] == "BKASSINE CIVIL DEFENCE CENTRE"
    assert c["map"]["centre_lat"] == pytest.approx(33.5530)
    assert c["features"] == {"thermal_camera": False, "drone": False, "wind_station": False}
    assert c["positions_are_placeholders"] is True
    assert [n["label"] for n in c["sites"]["nodes"]] == ["F-07-01", "F-07-02", "F-07-03", "F-08-01"]
    assert c["missing_after_minutes"] == 30


def test_status_during_alarm(client):
    s = client.get("/api/console/status").json()
    assert s["pill"] == "FIRE_ALARM" and s["pill_note"] is None
    by_label = {n["label"]: n for n in s["nodes"]}
    assert set(by_label) == {"F-07-02", "F-07-03"}  # the real nodes in the database
    n3 = by_label["F-07-03"]
    assert n3["state"] == "ALARM" and not n3["missing"]
    assert n3["dev_eui"] == NODE3.dev_eui  # kept for the detail view / tooltip
    assert n3["block"] == "F-07" and n3["lat"] is not None
    assert n3["signals"] == ["VOC up", "temperature up", "humidity down"]
    assert by_label["F-07-02"]["signals"] == []
    assert {n["label"] for n in s["never_seen"]} == {"F-07-01", "F-08-01"}

    net = s["network"]
    assert (net["total_nodes"], net["reporting"], net["missing"], net["in_alarm"]) == (2, 2, 0, 1)
    assert (net["gateways_online"], net["gateways_total"]) == (1, 1)
    assert s["gateways"][0]["label"] == "GW-01 ridge"

    assert [b["line"] for b in s["blocks"]] == ["Block F-07 - VOC up - temperature up - humidity down"]
    (zone,) = s["alarm_zones"]
    assert zone["label"] == "Block F-07 sensor coverage" and zone["nodes"] == ["F-07-03"]
    lats = [p[0] for p in zone["polygon"]]
    assert min(lats) < n3["lat"] < max(lats)


def test_status_has_no_device_eui_outside_the_dev_eui_fields(client):
    s = client.get("/api/console/status").json()
    for n in s["nodes"]:
        rest = {k: v for k, v in n.items() if k != "dev_eui"}
        assert n["dev_eui"] not in json.dumps(rest, default=str)
    assert NODE3.dev_eui not in json.dumps(s["blocks"]) + json.dumps(s["alarm_zones"])


def test_no_data_when_every_node_is_silent(engine):
    populate(engine, minutes=40)  # before the smoke: all NORMAL
    s = app_at(engine, START + timedelta(hours=3)).get("/api/console/status").json()
    assert s["pill"] == "NO_DATA"
    assert all(n["missing"] for n in s["nodes"])
    assert s["network"]["reporting"] == 0
    assert s["network"]["gateways_online"] == 0


def test_some_nodes_missing_is_said_out_loud(engine):
    populate(engine, minutes=40)
    late = START + timedelta(hours=3)
    model = NodeModel(NODE2, SIM, random.Random(1), START)
    smp = model.sample(late)
    env = build_uplink(application_id=SIM.application_id, application_name=SIM.application_name,
                       device_name=NODE2.name, dev_eui=NODE2.dev_eui, dev_addr=NODE2.dev_addr,
                       f_cnt=999, f_port=CFG.lorawan.heartbeat_f_port, payload=encode(smp.reading), rssi=-100, snr=1.0,
                       time=late, gateway_id=SIM.gateway_id)
    Ingestor(Store(engine)).handle(uplink_topic(SIM.application_id, NODE2.dev_eui),
                                   json.dumps(env).encode(), late)
    s = app_at(engine, late + timedelta(minutes=1)).get("/api/console/status").json()
    assert s["pill"] == "ALL_CLEAR"
    assert s["pill_note"] == "1 of 2 nodes missing"
    assert {n["label"]: n["missing"] for n in s["nodes"]} == {"F-07-02": False, "F-07-03": True}


def test_warming_up_is_not_all_clear(engine):
    populate(engine, minutes=10)  # fewer readings than the 30-reading warm-up
    s = app_at(engine, START + timedelta(minutes=10)).get("/api/console/status").json()
    assert {n["state"] for n in s["nodes"]} == {"WARMUP"}
    assert s["pill"] == "WARMING_UP"  # nobody is being monitored yet: never say ALL CLEAR
    assert s["network"]["warming_up"] == 2


def test_console_page_works_offline(client):
    page = client.get("/").text
    assert "/static/vendor/leaflet/leaflet.js" in page and "unpkg" not in page and "cdn" not in page
    for path in ["/static/vendor/leaflet/leaflet.js", "/static/vendor/leaflet/leaflet.css",
                 "/static/vendor/markercluster/leaflet.markercluster.js",
                 "/static/vendor/markercluster/MarkerCluster.css",
                 "/static/console.js", "/static/console.css"]:
        assert client.get(path).status_code == 200, path


def test_browser_never_keeps_an_old_script(client):
    import re
    page = client.get("/")
    assert page.headers["cache-control"] == "no-store"
    # Every script and stylesheet URL carries a version, so an update is fetched...
    urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', page.text)
    assert urls and all(re.search(r"\?v=[1-9]\d*$", u) for u in urls), urls
    # ...and the files themselves must be re-checked on each load.
    for u in urls:
        r = client.get(u)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", u


def test_alarm_outranks_silence(engine):
    populate(engine)  # node-3 ends in ALARM...
    s = app_at(engine, END + timedelta(hours=3)).get("/api/console/status").json()
    assert s["pill"] == "FIRE_ALARM"  # ...an alarm is never hidden behind NO DATA


HB_PORT, ALARM_PORT = CFG.lorawan.heartbeat_f_port, CFG.lorawan.alarm_mode_f_port


def _uplink(engine, node, t, f_cnt, port=HB_PORT):
    smp = NodeModel(node, SIM, random.Random(f_cnt), START).sample(t)
    env = build_uplink(application_id=SIM.application_id, application_name=SIM.application_name,
                       device_name=node.name, dev_eui=node.dev_eui, dev_addr=node.dev_addr,
                       f_cnt=f_cnt, f_port=port, payload=encode(smp.reading), rssi=-100, snr=1.0,
                       time=t, gateway_id=SIM.gateway_id)
    Ingestor(Store(engine)).handle(uplink_topic(SIM.application_id, node.dev_eui), json.dumps(env).encode(), t)
    return smp.reading


def _status(engine, at):
    return app_at(engine, at).get("/api/console/status").json()


def test_heartbeats_show_no_rapid_updates(engine):
    for i in range(3):
        _uplink(engine, NODE2, START + timedelta(minutes=10 * i), i)
    s = _status(engine, START + timedelta(minutes=21))
    assert not s["nodes"][0]["rapid_updates"]
    assert s["timing_warning"] is None  # the heartbeat matches this console's setting


def test_rapid_updates_come_from_the_alarm_mode_port_not_timing(engine):
    _uplink(engine, NODE2, START, 0)
    _uplink(engine, NODE2, START + timedelta(minutes=10), 1, port=ALARM_PORT)  # a heartbeat apart
    (n,) = _status(engine, START + timedelta(minutes=11))["nodes"]
    assert n["rapid_updates"]
    _uplink(engine, NODE2, START + timedelta(minutes=20), 2)  # back on its heartbeat
    (n,) = _status(engine, START + timedelta(minutes=21))["nodes"]
    assert not n["rapid_updates"]


def test_quick_heartbeats_are_not_rapid_updates_but_raise_a_setup_warning(engine):
    # The reported bug: simulator at DEMO_SPEEDUP=60 (10 s heartbeats), console at x1.
    for node in SIM.nodes:
        for i in range(3):
            _uplink(engine, node, START + timedelta(seconds=10 * i), i)
    s = _status(engine, START + timedelta(seconds=25))
    assert not any(n["rapid_updates"] for n in s["nodes"])
    assert "DEMO_SPEEDUP" in s["timing_warning"] and "~10 s" in s["timing_warning"]


def test_one_quick_gap_is_not_a_setup_warning(engine):
    for node in SIM.nodes:  # normal 10 min heartbeats...
        for i in range(2):
            _uplink(engine, node, START + timedelta(minutes=10 * i), i)
    _uplink(engine, NODE2, START + timedelta(minutes=10, seconds=5), 7)  # ...one simulator restart
    assert _status(engine, START + timedelta(minutes=11))["timing_warning"] is None


def test_alarm_mode_uplinks_do_not_raise_a_setup_warning(engine):
    for node in SIM.nodes:
        for i in range(3):
            _uplink(engine, node, START + timedelta(minutes=i), i, port=ALARM_PORT)
    assert _status(engine, START + timedelta(minutes=3))["timing_warning"] is None


def test_late_uplink_does_not_replace_the_latest_reading(engine):
    newest = _uplink(engine, NODE2, START + timedelta(minutes=20), 2)
    _uplink(engine, NODE2, START + timedelta(minutes=10), 1)  # delayed, arrives last
    (n,) = app_at(engine, START + timedelta(minutes=21)).get("/api/console/status").json()["nodes"]
    assert n["latest"]["temperature_c"] == pytest.approx(newest.temperature_c, abs=0.01)
    assert n["last_seen"].startswith("2026-07-01T08:20")


def test_status_query_count_does_not_grow_with_nodes(engine):
    from sqlalchemy import event

    def statements(n_nodes):
        for k, node in enumerate(SIM.nodes[:n_nodes]):
            _uplink(engine, node, START + timedelta(minutes=k), 100 + k)
        seen = []

        def count(conn, cursor, statement, *rest):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        try:
            app_at(engine, START + timedelta(minutes=5)).get("/api/console/status")
        finally:
            event.remove(engine, "before_cursor_execute", count)
        return seen

    one = len(statements(1))
    four = len(statements(4))
    assert 0 < one == four, (one, four)


def test_log_oldest_first_and_incremental(engine):
    store = Store(engine)
    store.append_log([clog.service_started(START, 0, ["console"], 1)]
                     + [clog.system_check(START + timedelta(minutes=i), {}, 1, 1) for i in range(1, 5)])
    c = app_at(engine, END)
    entries = c.get("/api/console/log", params={"limit": 3}).json()["entries"]
    assert len(entries) == 3
    assert [e["id"] for e in entries] == sorted(e["id"] for e in entries)  # newest at the bottom
    assert entries[-1]["time"].startswith("2026-07-01T08:04")
    last = entries[-1]["id"]
    assert c.get("/api/console/log", params={"after_id": last}).json()["entries"] == []
    store.append_log([clog.service_stopped(END)])
    new = c.get("/api/console/log", params={"after_id": last}).json()["entries"]
    assert [e["message"] for e in new] == ["Detection service stopped"]


def test_zone_hull_covers_every_alarmed_node():
    nodes = [{"label": s.label, "block": s.block, "lat": s.lat, "lon": s.lon}
             for s in CFG.sites.nodes if s.block == "F-07"]
    (zone,) = _alarm_zones(nodes, 150)
    poly = zone["polygon"]
    assert len(poly) >= 3
    for n in nodes:
        assert min(p[0] for p in poly) < n["lat"] < max(p[0] for p in poly)
        assert min(p[1] for p in poly) < n["lon"] < max(p[1] for p in poly)


def test_config_has_map_detail_settings(client):
    m = client.get("/api/console/config").json()["map"]
    assert m["node_detail_zoom"] > m["zoom"] and m["cluster_radius_px"] > 0


def test_map_blocks_take_the_worst_state(client):
    s = client.get("/api/console/status").json()
    blocks = {b["block"]: b for b in s["map_blocks"]}
    assert list(blocks) == ["F-07", "F-08"]  # worst first
    f07 = blocks["F-07"]
    assert f07["state"] == "ALARM" and f07["in_alarm"] == 1 and f07["alerting"] == ["F-07-03"]
    # Holds three registered nodes: two reporting, F-07-01 never heard from.
    assert (f07["nodes"], f07["reporting"], f07["never_seen"]) == (3, 2, 1)
    assert blocks["F-08"]["state"] == "NO_DATA" and blocks["F-08"]["nodes"] == 1
    # The block marker sits inside its area, and the area covers its nodes.
    lats = [p[0] for p in f07["area"]]
    assert min(lats) < f07["centre"][0] < max(lats)
    assert NODE3.dev_eui not in json.dumps(s["map_blocks"])


def test_network_count_is_the_database_not_the_map(client):
    s = client.get("/api/console/status").json()
    assert s["network"]["total_nodes"] == 2  # nodes in the database
    assert sum(b["nodes"] for b in s["map_blocks"]) == 4  # what the registry places on the map


def test_block_alarm_survives_silence(engine):
    populate(engine)  # node-3 ends in ALARM, then everything goes quiet
    s = app_at(engine, END + timedelta(hours=3)).get("/api/console/status").json()
    f07 = next(b for b in s["map_blocks"] if b["block"] == "F-07")
    assert f07["state"] == "ALARM" and f07["missing"] == 2


def test_block_with_silent_nodes_reads_missing(engine):
    populate(engine, minutes=40)  # all NORMAL, then quiet
    s = app_at(engine, START + timedelta(hours=3)).get("/api/console/status").json()
    assert next(b for b in s["map_blocks"] if b["block"] == "F-07")["state"] == "MISSING"


def _node(label, block, state="NORMAL", missing=False, lat=33.55, lon=35.56):
    return {"label": label, "block": block, "state": state, "missing": missing, "lat": lat, "lon": lon}


def test_hundreds_of_nodes_roll_up_by_block_field():
    from gateway.config import SiteNode
    sites = [SiteNode(dev_eui=f"{i:016x}", label=f"F-{b:02d}-{k:02d}", block=f"F-{b:02d}",
                      lat=33.50 + b * 0.004 + k * 0.0002, lon=35.55 + k * 0.0002)
             for i, (b, k) in enumerate((b, k) for b in range(1, 26) for k in range(1, 13))]
    shapes = _block_shapes(sites, 150)
    nodes = [_node(s.label, s.block, lat=s.lat, lon=s.lon) for s in sites]
    nodes[7 * 12 + 4]["state"] = "ALARM"  # one node, deep inside block F-08
    blocks = _map_blocks(shapes, nodes, [])
    assert len(blocks) == 25 and sum(b["nodes"] for b in blocks) == 300
    assert all(b["nodes"] == 12 for b in blocks)
    assert blocks[0]["block"] == "F-08" and blocks[0]["state"] == "ALARM"
    assert blocks[0]["alerting"] == ["F-08-05"]
    assert {b["state"] for b in blocks[1:]} == {"NORMAL"}


@pytest.mark.parametrize("states, worst", [
    (["NORMAL", "WARMUP"], "WARMUP"),
    (["NORMAL", "MISSING"], "MISSING"),
    (["MISSING", "WATCH"], "WATCH"),
    (["WATCH", "ALARM", "NORMAL"], "ALARM"),
])
def test_block_state_order(states, worst):
    nodes = [_node(f"X-{i}", "X", state=st if st != "MISSING" else "NORMAL", missing=st == "MISSING")
             for i, st in enumerate(states)]
    shapes = {"X": {"centre": [0, 0], "area": [], "registered": len(nodes)}}
    assert _map_blocks(shapes, nodes, [])[0]["state"] == worst


def test_unplaced_alarm_is_reported():
    nodes = [_node("node-9", None, state="ALARM", lat=None, lon=None), _node("F-07-01", "F-07")]
    u = _unplaced(nodes)
    assert (u["count"], u["in_alarm"], u["alerting"]) == (1, 1, ["node-9"])


def test_hull_of_square_drops_inner_point():
    pts = [(0, 0), (0, 1), (1, 1), (1, 0), (0.5, 0.5)]
    assert sorted(map(tuple, _hull(pts))) == [(0, 0), (0, 1), (1, 0), (1, 1)]
