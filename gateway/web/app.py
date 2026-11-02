"""Read-only web dashboard.

The web app only READS the database, through the read-only connection
(database.web_url: the "webapp_ro" Postgres role, or SQLite opened in
read-only mode). It never creates tables and has no write endpoints.
Detection and alerting run in the separate detection service and do not
depend on anyone having this page open.

Pages (plain HTML + JS, no build step):  /   /node/{dev_eui}   /alarms
JSON API:  /api/config  /api/status  /api/nodes/{dev_eui}/readings  /api/events
Operator console API:  /api/console/config  /api/console/status  /api/console/log
(see gateway/web/console.py)
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Connection, Engine, and_, select
from sqlalchemy.exc import DBAPIError

from gateway.config import Config
from gateway.db import events, make_engine, nodes, readings, utcnow
from gateway.web.console import add_console_routes

STATIC = Path(__file__).parent / "static"
MAX_POINTS = 1500  # per chart request; older points are thinned out
MAX_HOURS = 24 * 31
NO_STORE = {"Cache-Control": "no-store"}
_ASSET_URL = re.compile(r'((?:src|href)="/static/)([^"?#]+)"')


class _RevalidatingStatic(StaticFiles):
    """Scripts and styles: the browser must check for a newer version every
    time (a cheap 304 when unchanged). Without this, browsers keep using an
    old console.js after an update."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(
    cfg: Config,
    engine: Engine | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> FastAPI:
    engine = engine or make_engine(cfg.database.web_url, read_only=True)
    det = cfg.detection
    app = FastAPI(title="Wildfire gateway", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", _RevalidatingStatic(directory=STATIC), name="static")

    def query(fn: Callable[[Connection], Any]) -> Any:
        try:
            with engine.connect() as conn:
                return fn(conn)
        except DBAPIError as e:
            msg = str(e.orig).strip().splitlines()[0] if str(e.orig).strip() else type(e).__name__
            raise HTTPException(
                503, f"database unavailable ({msg}). Has the detection service run yet?"
            ) from e

    # --- pages -------------------------------------------------------------

    def page(name: str) -> HTMLResponse:
        """The page, with each /static/ file it uses stamped with that file's
        modification time (?v=...): after an update the browser fetches the new
        file, even one it cached before this fix."""
        def stamp(m: re.Match) -> str:
            f = STATIC / m.group(2)
            return f'{m.group(1)}{m.group(2)}?v={f.stat().st_mtime_ns if f.is_file() else 0}"'

        html = (STATIC / name).read_text(encoding="utf-8")
        return HTMLResponse(_ASSET_URL.sub(stamp, html), headers=NO_STORE)

    @app.get("/", include_in_schema=False)
    def dashboard_page():
        return page("index.html")

    @app.get("/node/{dev_eui}", include_in_schema=False)
    def node_page(dev_eui: str):
        return page("node.html")

    @app.get("/alarms", include_in_schema=False)
    def alarms_page():
        return page("alarms.html")

    # --- API ---------------------------------------------------------------

    @app.get("/api/config")
    def api_config():
        return {
            "poll_seconds": cfg.web.poll_seconds,
            "chart_hours": cfg.web.chart_hours,
            "offline_after_minutes": det.offline_after_minutes,
            "watch": det.watch.model_dump(),
            "alarm": det.alarm.model_dump(),
        }

    @app.get("/api/status")
    def api_status():
        """Every node with its latest reading, plus the alarm-banner summary."""
        now = clock()

        def fetch(conn: Connection):
            out = []
            for n in conn.execute(select(nodes).order_by(nodes.c.name)).all():
                r = conn.execute(
                    select(readings).where(readings.c.dev_eui == n.dev_eui)
                    .order_by(readings.c.time.desc()).limit(1)
                ).first()
                out.append(_node_json(n, r))
            return out

        node_list = query(fetch)
        latest = max((n["last_seen"] for n in node_list), default=None)
        stale_after = timedelta(minutes=det.offline_after_minutes)
        return {
            "generated_at": now,
            "nodes": node_list,
            "summary": {
                "alarm": [n["name"] for n in node_list if n["state"] == "ALARM"],
                "watch": [n["name"] for n in node_list if n["state"] == "WATCH"],
                "offline": [n["name"] for n in node_list if not n["online"]],
                "latest_data_at": latest,
                # No data from ANY node: likely the detection service, broker or
                # gateway is down, not the nodes. Worth shouting about.
                "stale": latest is None or now - latest > stale_after,
            },
        }

    @app.get("/api/nodes/{dev_eui}/readings")
    def api_readings(dev_eui: str, hours: float | None = Query(default=None, gt=0, le=MAX_HOURS)):
        hours = hours or cfg.web.chart_hours
        now = clock()
        since = now - timedelta(hours=hours)

        def fetch(conn: Connection):
            n = conn.execute(select(nodes).where(nodes.c.dev_eui == dev_eui.lower())).first()
            if n is None:
                return None, []
            rows = conn.execute(
                select(readings).where(and_(readings.c.dev_eui == n.dev_eui,
                                            readings.c.time >= since))
                .order_by(readings.c.time)
            ).all()
            return n, rows

        n, rows = query(fetch)
        if n is None:
            raise HTTPException(404, f"unknown node {dev_eui}")
        return {
            "node": _node_json(n, rows[-1] if rows else None),
            "from": since,
            "to": now,
            "total_points": len(rows),
            "points": [_point_json(r) for r in _thin(rows)],
        }

    @app.get("/api/events")
    def api_events(
        kind: Literal["alarms", "connectivity", "all"] = "alarms",
        dev_eui: str | None = None,
        limit: int = Query(default=200, ge=1, le=2000),
    ):
        stmt = (select(events, nodes.c.name)
                .join(nodes, nodes.c.dev_eui == events.c.dev_eui)
                .order_by(events.c.time.desc(), events.c.id.desc()).limit(limit))
        if kind == "alarms":
            # Raised (WATCH/ALARM) and cleared (back to NORMAL after one).
            stmt = stmt.where(events.c.kind == "STATE", events.c.from_state != "WARMUP")
        elif kind == "connectivity":
            stmt = stmt.where(events.c.kind.in_(["OFFLINE", "ONLINE"]))
        if dev_eui:
            stmt = stmt.where(events.c.dev_eui == dev_eui.lower())
        rows = query(lambda conn: conn.execute(stmt).all())
        return {"events": [
            {
                "id": e.id, "time": e.time, "dev_eui": e.dev_eui, "node": e.name,
                "kind": e.kind, "from_state": e.from_state, "to_state": e.to_state,
                "message": e.message, "notified": e.notified, "details": e.details,
            }
            for e in rows
        ]}

    add_console_routes(app, cfg, query, clock)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        query(lambda conn: conn.execute(select(nodes.c.dev_eui).limit(1)).all())
        return {"ok": True}

    return app


def _thin(rows: list) -> list:
    """Keep at most ~MAX_POINTS evenly spaced points, but never drop a reading
    taken while the node was in WATCH or ALARM."""
    if len(rows) <= MAX_POINTS:
        return rows
    step = math.ceil(len(rows) / MAX_POINTS)
    return [r for i, r in enumerate(rows)
            if i % step == 0 or i == len(rows) - 1 or r.state in ("WATCH", "ALARM")]


def _node_json(n, r) -> dict[str, Any]:
    latest = None
    if r is not None:
        latest = _point_json(r)
        latest["gas_vs_baseline_pct"] = (
            (r.gas_resistance_ohm / r.baseline_gas_ohm - 1) * 100 if r.baseline_gas_ohm else None)
    return {
        "dev_eui": n.dev_eui,
        "name": n.name,
        "state": n.state,
        "state_since": n.state_since,
        "online": n.online,
        "last_seen": n.last_seen,
        "rssi": n.last_rssi,
        "snr": n.last_snr,
        "battery_v": n.battery_v,
        "latest": latest,
    }


def _point_json(r) -> dict[str, Any]:
    return {
        "t": r.time,
        "temperature_c": r.temperature_c,
        "humidity_pct": r.humidity_pct,
        "pressure_hpa": r.pressure_hpa,
        "gas_ohm": r.gas_resistance_ohm,
        "gas_saturated": r.gas_saturated,
        "battery_v": r.battery_v,
        "rssi": r.rssi,
        "snr": r.snr,
        "baseline_temperature_c": r.baseline_temperature_c,
        "baseline_humidity_pct": r.baseline_humidity_pct,
        "baseline_gas_ohm": r.baseline_gas_ohm,
        "state": r.state,
    }
