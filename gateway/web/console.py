"""Operator console API (read-only), used by the console page at /.

    /api/console/config   title, map, feature flags, site registry
    /api/console/status   status pill, nodes, gateways, blocks, map blocks, alarm zones
    /api/console/log      event log, oldest first; ?after_id= for new lines

The map draws per-block aggregates when zoomed out ("map_blocks": one per
block in the site registry, with its node count and the WORST state of any
node in it, so an alarm is never hidden by zooming out) and individual nodes
when zoomed in. Network counts never depend on what the map draws.

Operators see site labels; each node still carries its dev_eui for the node
detail page. Every node and gateway count comes from the database. The site
registry only adds names and map positions (positions are PLACEHOLDERS until
a site survey; see config.yaml).

"Missing" is judged here from last_seen, with the same threshold as the
detection service's OFFLINE alert (heartbeat x missed heartbeats), so the
page is honest even when the detection service itself is down.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, Query
from sqlalchemy import Connection, func, select

from gateway.config import Config
from gateway.db import console_log, gateways, nodes, readings

ALARM, WATCH, WARMUP = "ALARM", "WATCH", "WARMUP"
PILL_ALARM, PILL_WATCH, PILL_CLEAR, PILL_NO_DATA = "FIRE_ALARM", "WATCH", "ALL_CLEAR", "NO_DATA"
PILL_WARMING_UP = "WARMING_UP"  # data arriving, but no node is monitored yet
MISSING, NO_DATA = "MISSING", "NO_DATA"
# A block shows the worst state of its nodes, in this order. A node on WATCH
# or in ALARM keeps that state even when it falls silent (fire destroys nodes).
BLOCK_STATE_ORDER = [ALARM, WATCH, MISSING, WARMUP, "NORMAL", NO_DATA]
CIRCLE_POINTS = 24


def add_console_routes(app: FastAPI, cfg: Config, query: Callable[[Callable[[Connection], Any]], Any],
                       clock: Callable[[], datetime]) -> None:
    sites, con, det, timing = cfg.sites, cfg.console, cfg.detection, cfg.timing
    missing_after = timedelta(minutes=timing.missing_after_minutes)
    heartbeat = timedelta(minutes=timing.heartbeat_minutes)
    alarm_port = cfg.lorawan.alarm_mode_f_port
    block_shapes = _block_shapes(sites.nodes, con.map.alarm_zone_radius_m)  # the registry is fixed

    @app.get("/api/console/config")
    def console_config():
        return {
            "title": con.title,
            "subtitle": con.subtitle,
            "poll_seconds": cfg.web.poll_seconds,
            "heartbeat_minutes": timing.heartbeat_minutes,
            "missing_after_minutes": timing.missing_after_minutes,
            "demo_speedup": timing.demo_speedup,
            "map": con.map.model_dump(),
            "features": con.features.model_dump(),
            "positions_are_placeholders": True,
            "sites": {
                "nodes": [n.model_dump() for n in sites.nodes],
                "gateways": [g.model_dump() for g in sites.gateways],
                "centre": sites.centre.model_dump(),
            },
        }

    @app.get("/api/console/status")
    def console_status():
        now = clock()

        def fetch(conn: Connection):
            # Three queries whatever the node count (this runs every few seconds
            # per open console). A node's last_seen is the time of its newest
            # reading, so that reading is found by an indexed join, not a
            # query per node.
            node_rows = conn.execute(select(nodes)).all()
            latest = {}
            for r in conn.execute(
                select(readings).join(nodes, (readings.c.dev_eui == nodes.c.dev_eui)
                                      & (readings.c.time == nodes.c.last_seen))
                .order_by(readings.c.id)
            ):
                latest[r.dev_eui] = r  # same-time duplicates: the last stored wins
            # The reading before it (recent nodes only), for the timing check.
            prev = dict(conn.execute(
                select(readings.c.dev_eui, func.max(readings.c.time))
                .join(nodes, readings.c.dev_eui == nodes.c.dev_eui)
                .where(readings.c.time < nodes.c.last_seen,
                       readings.c.time >= now - missing_after - 2 * heartbeat)
                .group_by(readings.c.dev_eui)
            ).all())
            return node_rows, latest, prev, conn.execute(select(gateways)).all()

        node_rows, latest, prev, gw_rows = query(fetch)
        node_list = sorted((_node(n, latest.get(n.dev_eui), now) for n in node_rows),
                           key=lambda n: (n["label"]))
        seen = {n["dev_eui"] for n in node_list}
        never_seen = [{"dev_eui": s.dev_eui, "label": s.label, "block": s.block,
                       "lat": s.lat, "lon": s.lon} for s in sites.nodes if s.dev_eui not in seen]
        gw_list = _gateways(gw_rows, now)

        alarm = [n for n in node_list if n["state"] == ALARM]
        watch = [n for n in node_list if n["state"] == WATCH]
        missing = [n for n in node_list if n["missing"]]
        reporting = len(node_list) - len(missing)
        warming = [n for n in node_list if not n["missing"] and n["state"] == WARMUP]
        if alarm:
            pill = PILL_ALARM
        elif watch:
            pill = PILL_WATCH
        elif reporting == 0:
            pill = PILL_NO_DATA  # nothing heard from any node: gateway, broker or service down
        elif len(warming) == reporting:
            pill = PILL_WARMING_UP  # "all clear" would claim monitoring that is not happening yet
        else:
            pill = PILL_CLEAR
        notes = []
        if missing and reporting:
            notes.append(f"{len(missing)} of {len(node_list)} nodes missing")
        if warming and pill != PILL_WARMING_UP:
            notes.append(f"{len(warming)} warming up")
        note = ", ".join(notes) or None

        return {
            "generated_at": now,
            "pill": pill,
            "pill_note": note,
            "nodes": node_list,
            "never_seen": never_seen,
            "gateways": gw_list,
            "network": {
                "total_nodes": len(node_list),
                "reporting": reporting,
                "missing": len(missing),
                "in_alarm": len(alarm),
                "on_watch": len(watch),
                "warming_up": len(warming),
                "gateways_online": sum(g["online"] for g in gw_list),
                "gateways_total": len(gw_list),
                "latest_data_at": max((n["last_seen"] for n in node_list), default=None),
            },
            "blocks": _blocks(node_list),
            "map_blocks": _map_blocks(block_shapes, node_list, never_seen),
            "unplaced": _unplaced(node_list),
            "alarm_zones": _alarm_zones(alarm, con.map.alarm_zone_radius_m),
            "timing_warning": _timing_warning(latest, prev, now),
        }

    def _timing_warning(latest: dict, prev: dict, now: datetime) -> str | None:
        """Heartbeats arriving far faster than THIS app's heartbeat setting mean
        the processes run with different DEMO_SPEEDUP values (or the firmware's
        heartbeat differs from config.yaml). Times shown here would be wrong."""
        gaps = [r.time - prev[d] for d, r in latest.items()
                if d in prev and r.f_port != alarm_port and now - r.time <= missing_after]
        if not gaps:
            return None
        too_fast = sum(g < heartbeat / 3 for g in gaps)
        if too_fast * 2 < len(gaps) or (len(gaps) > 1 and too_fast < 2):
            return None  # one restart or late packet is not a pattern
        typical = sorted(gaps)[len(gaps) // 2].total_seconds()
        return (f"Nodes report every ~{typical:.0f} s, but this console expects a heartbeat every "
                f"{timing.heartbeat_minutes * 60:.0f} s. DEMO_SPEEDUP probably differs between "
                f"windows (this console: x{timing.demo_speedup:g}). Set the same value everywhere "
                f"and restart.")

    @app.get("/api/console/log")
    def console_log_api(after_id: int | None = Query(default=None, ge=0),
                        limit: int = Query(default=200, ge=1, le=1000)):
        """The newest `limit` lines (or those after `after_id`), oldest first."""
        stmt = select(console_log)
        if after_id is not None:
            stmt = stmt.where(console_log.c.id > after_id).order_by(console_log.c.id).limit(limit)
            rows = query(lambda conn: conn.execute(stmt).all())
        else:
            stmt = stmt.order_by(console_log.c.id.desc()).limit(limit)
            rows = list(reversed(query(lambda conn: conn.execute(stmt).all())))
        return {"entries": [
            {"id": r.id, "time": r.time, "level": r.level, "category": r.category,
             "message": r.message, "block": r.block, "dev_eui": r.dev_eui}
            for r in rows
        ]}

    def _node(n, r, now: datetime) -> dict[str, Any]:
        site = sites.node(n.dev_eui)
        signals = _signals(r) if r is not None and n.state in (WATCH, ALARM) else []
        # The node says it is in alarm mode by sending on the alarm-mode port
        # (its own sensor saw a change). Never guessed from timing. Only
        # meaningful while it is still transmitting.
        rapid = (r is not None and r.f_port == alarm_port and not now - n.last_seen > missing_after)
        return {
            "dev_eui": n.dev_eui,
            "label": site.label if site else n.name,
            "block": site.block if site else None,
            "device_name": n.name,
            "lat": site.lat if site else None,
            "lon": site.lon if site else None,
            "state": n.state,
            "state_since": n.state_since,
            "last_seen": n.last_seen,
            "seconds_since_seen": max((now - n.last_seen).total_seconds(), 0.0),
            "missing": now - n.last_seen > missing_after,
            "rapid_updates": rapid,
            "battery_v": n.battery_v,
            "rssi": n.last_rssi,
            "snr": n.last_snr,
            "latest": None if r is None else {
                "temperature_c": r.temperature_c,
                "humidity_pct": r.humidity_pct,
                "gas_vs_baseline_pct": ((r.gas_resistance_ohm / r.baseline_gas_ohm - 1) * 100
                                        if r.baseline_gas_ohm else None),
                "temperature_vs_baseline_c": (r.temperature_c - r.baseline_temperature_c
                                              if r.baseline_temperature_c is not None else None),
                "humidity_vs_baseline_pct": (r.humidity_pct - r.baseline_humidity_pct
                                             if r.baseline_humidity_pct is not None else None),
            },
            "signals": signals,
        }

    def _signals(r) -> list[str]:
        """Which channels are past their WATCH threshold, in operator words."""
        w, out = det.watch, []
        if r.baseline_gas_ohm and (1 - r.gas_resistance_ohm / r.baseline_gas_ohm) * 100 >= w.gas_drop_pct:
            out.append("VOC up")  # BME688 gas resistance falls as VOCs/smoke rise
        if r.baseline_temperature_c is not None and r.temperature_c - r.baseline_temperature_c >= w.temperature_rise_c:
            out.append("temperature up")
        if r.baseline_humidity_pct is not None and r.baseline_humidity_pct - r.humidity_pct >= w.humidity_drop_pct:
            out.append("humidity down")
        return out

    def _gateways(rows, now: datetime) -> list[dict[str, Any]]:
        by_id = {r.gateway_id: r for r in rows}
        out = []
        for gid in [g.gateway_id for g in sites.gateways] + sorted(set(by_id) - {g.gateway_id for g in sites.gateways}):
            site = next((g for g in sites.gateways if g.gateway_id == gid), None)
            r = by_id.get(gid)
            out.append({
                "gateway_id": gid,
                "label": site.label if site else gid,
                "lat": site.lat if site else None,
                "lon": site.lon if site else None,
                "last_seen": r.last_seen if r else None,
                # A gateway relays every node's heartbeat: silent this long means down.
                "online": r is not None and now - r.last_seen <= missing_after,
                "uplinks": r.uplinks if r else 0,
            })
        return out


def _blocks(node_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blocks with a node on WATCH or in ALARM, worst first, with their signals:
    {"block": "F-07", "state": "ALARM", "nodes": [...], "signals": [...],
     "line": "Block F-07 - VOC up - temperature up - humidity down"}"""
    order = ["VOC up", "temperature up", "humidity down"]
    out: dict[str, dict[str, Any]] = {}
    for n in node_list:
        if n["state"] not in (WATCH, ALARM):
            continue
        key = n["block"] or n["label"]
        b = out.setdefault(key, {"block": key, "state": WATCH, "nodes": [], "signals": set()})
        if n["state"] == ALARM:
            b["state"] = ALARM
        b["nodes"].append(n["label"])
        b["signals"] |= set(n["signals"])
    result = []
    for b in sorted(out.values(), key=lambda b: (b["state"] != ALARM, b["block"])):
        b["signals"] = [s for s in order if s in b["signals"]]
        b["line"] = " - ".join([f"Block {b['block']}", *b["signals"]])
        result.append(b)
    return result


def _block_shapes(site_nodes, radius_m: float) -> dict[str, dict[str, Any]]:
    """Per registry block: its centre (mean node position) and area (hull of a
    coverage circle around each node). Grouped by the `block` field only, so
    adding nodes to config.yaml needs no UI change."""
    groups: dict[str, list] = {}
    for s in site_nodes:
        groups.setdefault(s.block, []).append(s)
    return {block: {
        "centre": [sum(s.lat for s in ns) / len(ns), sum(s.lon for s in ns) / len(ns)],
        "area": _hull([p for s in ns for p in _circle(s.lat, s.lon, radius_m)]),
        "registered": len(ns),
    } for block, ns in groups.items()}


def _node_map_state(n: dict[str, Any]) -> str:
    if n["state"] in (ALARM, WATCH):
        return n["state"]  # never downgraded by silence
    return MISSING if n["missing"] else n["state"]


def _map_blocks(shapes: dict[str, dict[str, Any]], node_list: list[dict[str, Any]],
                never_seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One aggregate per registry block, worst state first:
    {"block", "state", "nodes", "reporting", "missing", "never_seen", "in_alarm",
     "on_watch", "warming_up", "alerting": [labels on WATCH/ALARM], "centre", "area"}"""
    rank = {s: i for i, s in enumerate(BLOCK_STATE_ORDER)}
    out = []
    for block, shape in shapes.items():
        members = [n for n in node_list if n["block"] == block]
        states = [_node_map_state(n) for n in members]
        unseen = sum(1 for n in never_seen if n["block"] == block)
        out.append({
            "block": block,
            "state": min(states, key=lambda s: rank.get(s, len(rank))) if states else NO_DATA,
            "nodes": len(members) + unseen,
            "reporting": sum(not n["missing"] for n in members),
            "missing": sum(n["missing"] for n in members),
            "never_seen": unseen,
            "in_alarm": states.count(ALARM),
            "on_watch": states.count(WATCH),
            "warming_up": states.count(WARMUP),
            "alerting": sorted(n["label"] for n in members if n["state"] in (ALARM, WATCH)),
            "centre": shape["centre"],
            "area": shape["area"],
        })
    out.sort(key=lambda b: (rank.get(b["state"], len(rank)), b["block"]))
    return out


def _unplaced(node_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Nodes the map cannot draw (no site entry). Reported so an alarm on one is
    still shown on the map, as a note."""
    ns = [n for n in node_list if n["lat"] is None]
    return {"count": len(ns), "in_alarm": sum(n["state"] == ALARM for n in ns),
            "on_watch": sum(n["state"] == WATCH for n in ns),
            "alerting": sorted(n["label"] for n in ns if n["state"] in (ALARM, WATCH))}


def _alarm_zones(alarm_nodes: list[dict[str, Any]], radius_m: float) -> list[dict[str, Any]]:
    """One polygon per block: the convex hull of a circle around each node in
    ALARM. This is the area the SENSORS cover, not the extent of the fire."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for n in alarm_nodes:
        if n["lat"] is not None:
            groups.setdefault(n["block"] or n["label"], []).append(n)
    zones = []
    for block, ns in sorted(groups.items()):
        pts = [p for n in ns for p in _circle(n["lat"], n["lon"], radius_m)]
        zones.append({"block": block, "nodes": sorted(n["label"] for n in ns),
                      "label": f"Block {block} sensor coverage", "polygon": _hull(pts)})
    return zones


def _circle(lat: float, lon: float, radius_m: float) -> list[tuple[float, float]]:
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
    return [(lat + dlat * math.sin(a), lon + dlon * math.cos(a))
            for a in (2 * math.pi * i / CIRCLE_POINTS for i in range(CIRCLE_POINTS))]


def _hull(points: list[tuple[float, float]]) -> list[list[float]]:
    """Convex hull (Andrew's monotone chain), as [[lat, lon], ...] counter-clockwise."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return [list(p) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    upper: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]
