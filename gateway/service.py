"""Headless detection service: MQTT -> decode -> store -> detect -> alert.

    python -m gateway.service [--config PATH]

Runs on its own, independent of the web app. Nobody needs to have a browser
open for readings to be stored and alarms to be raised.

Nodes are named by their site label (config.yaml "sites", e.g. F-07-03) in
events, alerts and the console log; the devEUI stays in the database.
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, OperationalError

from gateway import console_log as clog
from gateway.codec import SensorReading
from gateway.config import Config, load_config
from gateway.db import init_schema, make_engine, utcnow
from gateway.detect import DetectionEngine, Event
from gateway.ingest import Ingestor
from gateway.mqtt import BrokerUnavailable, connect
from gateway.notify import Dispatcher, NotifierConfigError, build_notifiers, headline
from gateway.store import Store

log = logging.getLogger("gateway.service")

DB_RETRY_SECONDS = 2.0
HEARTBEAT_SECONDS = 300.0
OFFLINE_CHECK_SECONDS = 30.0

T = TypeVar("T")


def _first_line(e: DBAPIError) -> str:
    return str(e.orig).strip().splitlines()[0] if str(e.orig).strip() else type(e).__name__


def _retry(what: str, fn: Callable[[], T]) -> T | None:
    """A database outage must not lose readings or alarms: keep retrying.
    Any other database error is a bad message: log it and return None."""
    while True:
        try:
            return fn()
        except OperationalError as e:
            log.error("database unavailable during %s (%s); retrying in %gs",
                      what, _first_line(e), DB_RETRY_SECONDS)
        except DBAPIError as e:
            if not e.connection_invalidated:
                log.exception("database rejected %s; skipping it", what)
                return None
        time.sleep(DB_RETRY_SECONDS)


def _label(cfg: Config, dev_eui: str, device_name: str) -> str:
    """What operators call a node: its site label, else the ChirpStack device name."""
    return cfg.sites.label_for(dev_eui, device_name)


def _log_events(store: Store, cfg: Config, tracker: clog.BlockTracker, engine: DetectionEngine,
                evs: Sequence[Event], t: datetime) -> None:
    """Node events, plus any change in the block picture, to the console log."""
    entries = clog.node_entries(evs, cfg.sites)
    if evs:
        entries += tracker.update(engine.nodes, t)
    _retry("writing the console log", lambda: store.append_log(entries))


def _gateways_online(cfg: Config, store: Store, now: datetime) -> tuple[int, int]:
    """(heard within the missing threshold, configured or ever seen)."""
    rows = store.all_gateways()
    cutoff = now - timedelta(minutes=cfg.timing.missing_after_minutes)
    known = {g.gateway_id for g in cfg.sites.gateways} | {r.gateway_id for r in rows}
    return sum(r.last_seen >= cutoff for r in rows), len(known)


def _alert(dispatcher: Dispatcher, evs: Sequence[Event], ids: Sequence[int]) -> None:
    """Log every event; hand the ones that warrant a message to the notifiers."""
    for e, event_id in zip(evs, ids):
        if e.notify:
            log.warning("ALERT  %s", e.message)
            dispatcher.submit(e, event_id)
        else:
            log.info("event  %s", e.message)


def _replay(cfg: Config, store: Store, engine: DetectionEngine, now: datetime | None = None) -> None:
    """Rebuild baselines and states from stored readings so a restart does not
    mean hours of warm-up. Replayed transitions are not re-alerted."""
    since = (now or utcnow()) - timedelta(hours=cfg.detection.replay_hours_on_startup)
    rows = store.readings_since(since)
    for r in rows:
        engine.process(r.dev_eui, _label(cfg, r.dev_eui, r.name), r.time, SensorReading(
            r.temperature_c, r.humidity_pct, r.pressure_hpa, r.gas_resistance_ohm,
            r.battery_v, r.gas_saturated))
    for n in store.all_nodes():
        d = engine.node(n.dev_eui, _label(cfg, n.dev_eui, n.name))
        if d.last_time is None:  # known node, silent for the whole replay window
            d.last_time = n.last_seen
        d.online = n.online
    store.save_node_states(engine.nodes.values())
    log.info("replayed %d readings from the last %gh: %s", len(rows),
             cfg.detection.replay_hours_on_startup,
             ", ".join(f"{d.name}={d.state}" for d in engine.nodes.values()) or "no nodes yet")


def run(cfg: Config) -> int:
    try:
        notifiers = build_notifiers(cfg.notifiers)
    except NotifierConfigError as e:
        log.error("notifier setup: %s", e)
        return 1
    log.info("notifiers: %s", ", ".join(n.name for n in notifiers) or "none")

    db = make_engine(cfg.database.url)
    log.info("database: %s", db.url.render_as_string(hide_password=True))
    _retry("schema setup", lambda: init_schema(db))
    store = Store(db)
    ingestor = Ingestor(store)
    engine = DetectionEngine(cfg.detection)
    _retry("startup replay", lambda: _replay(cfg, store, engine))
    tracker = clog.BlockTracker(cfg.sites)
    tracker.prime(engine.nodes)

    def on_result(e: Event, _id: int, channel: str, ok: bool, detail: str) -> None:
        entry = clog.notification(utcnow(), headline(e), e.node_name, channel, ok, detail, e.dev_eui)
        _retry("writing the console log", lambda: store.append_log([entry]))

    dispatcher = Dispatcher(notifiers, on_delivered=lambda event_id: _retry(
        "marking event notified", lambda: store.mark_notified([event_id])),
        on_result=on_result).start()

    # paho delivers messages on its own thread; process them here, one at a time.
    inbox: queue.Queue[tuple[str, bytes, object]] = queue.Queue()
    try:
        client = connect(cfg.mqtt, "wildfire-service", [cfg.mqtt.uplink_topic],
                         lambda msg: inbox.put((msg.topic, msg.payload, utcnow())))
    except BrokerUnavailable as e:
        log.error("%s", e)
        return 1

    log.info("detection service running, listening on %s. Ctrl+C to stop.", cfg.mqtt.uplink_topic)
    _retry("writing the console log", lambda: store.append_log([clog.service_started(
        utcnow(), len(engine.nodes), [n.name for n in notifiers], cfg.timing.demo_speedup)]))
    next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
    # Offline checks run often enough for a demo speed-up's short thresholds.
    offline_every = min(OFFLINE_CHECK_SECONDS, cfg.timing.heartbeat_minutes * 60 / 2)
    next_offline_check = time.monotonic() + offline_every
    system_check_every = cfg.timing.system_check_minutes * 60
    next_system_check = time.monotonic() + system_check_every
    try:
        while True:
            try:
                topic, body, received_at = inbox.get(timeout=1.0)
            except queue.Empty:
                pass
            else:
                ingested = _retry(f"message on {topic}",
                                  lambda: ingestor.handle(topic, body, received_at))
                if ingested is not None:
                    up = ingested.uplink
                    ev = engine.process(up.dev_eui, _label(cfg, up.dev_eui, up.device_name),
                                        up.time, ingested.reading)
                    node = engine.nodes[up.dev_eui]
                    ids = _retry("saving detection result",
                                 lambda: store.save_evaluation(ingested.reading_id, ev, node))
                    if ids is not None:
                        _alert(dispatcher, ev.events, ids)
                        _log_events(store, cfg, tracker, engine, ev.events, up.time)

            now_m = time.monotonic()
            if now_m >= next_offline_check:
                next_offline_check = now_m + offline_every
                now = utcnow()
                offline = engine.check_offline(now)
                if offline:
                    ids = _retry("saving offline events", lambda: store.save_events(offline, engine.nodes))
                    if ids is not None:
                        _alert(dispatcher, offline, ids)
                        _log_events(store, cfg, tracker, engine, offline, now)

            if now_m >= next_system_check:
                next_system_check = now_m + system_check_every
                now = utcnow()
                gw = _retry("reading gateways", lambda: _gateways_online(cfg, store, now)) or (0, 0)
                _retry("writing the console log", lambda: store.append_log(
                    [clog.system_check(now, engine.nodes, *gw)]))

            if now_m >= next_heartbeat:
                next_heartbeat = now_m + HEARTBEAT_SECONDS
                s = ingestor.stats
                log.info("alive: %d stored, %d duplicates, %d rejected, %d queued; %s",
                         s.stored, s.duplicates, s.rejected, inbox.qsize(),
                         ", ".join(f"{d.name}={d.state}" for d in engine.nodes.values()))
    except KeyboardInterrupt:
        log.info("stopping (sending any queued alerts first)")
        return 0
    finally:
        client.loop_stop()
        client.disconnect()
        dispatcher.stop()
        try:  # best effort: the database may be the reason we are stopping
            store.append_log([clog.service_stopped(utcnow())])
        except DBAPIError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway.service", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="path to config.yaml (default: ./config.yaml)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    # httpx logs full request URLs at INFO; the Telegram URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return run(load_config(args.config))


if __name__ == "__main__":
    sys.exit(main())
