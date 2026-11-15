"""Headless detection service: MQTT -> decode -> detect -> alert, then store.

    python -m gateway.service [--config PATH]

Runs on its own, independent of the web app. Nobody needs to have a browser
open for readings to be stored and alarms to be raised.

Detection and alerting work from memory and come FIRST; every database write
goes through a queue (gateway/writer.py) with its own thread. So while the
service runs, a database outage does not stop detection or alerts: the
writes wait and are written in order when the database is back. Two limits:

- The service cannot START without the database: it rebuilds every node's
  baseline from stored readings (otherwise each node would need a fresh
  warm-up). Start-up waits until the database answers.
- The operator console (the web app) reads only the database, so during an
  outage it shows "cannot reach the database". Alerts (console window,
  Telegram) keep going out.

MQTT: a persistent session (mqtt.service_client_id), so the broker queues
uplinks while the service is down. Redeliveries are dropped by
Ingestor.accept before detection.

Nodes are named by their site label (config.yaml "sites", e.g. F-07-03) in
events, alerts and the console log; the devEUI stays in the database.
"""

from __future__ import annotations

import argparse
import copy
import logging
import queue
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, OperationalError

from gateway import console_log as clog
from gateway.codec import SensorReading, ControlMessage
from gateway.commands import CommandSettings, start_command_server, unavailable
from gateway.config import Config, load_config
from gateway.db import init_schema, make_engine, utcnow
from gateway.detect import DetectionEngine, Event
from gateway.ingest import Accepted, Ingestor, log_reading
from gateway.mqtt import BrokerUnavailable, connect
from gateway.notify import Dispatcher, NotifierConfigError, build_notifiers, headline
from gateway.store import NodeState, Store, uplink_key
from gateway.writer import ClearAlarmJob, ControlJob, EventRef, EventsJob, LogJob, NotifiedJob, ReadingJob, Writer

log = logging.getLogger("gateway.service")

DB_RETRY_SECONDS = 2.0
HEARTBEAT_SECONDS = 300.0
OFFLINE_CHECK_SECONDS = 30.0
STOP_FLUSH_SECONDS = 10.0

T = TypeVar("T")


def _first_line(e: DBAPIError) -> str:
    return str(e.orig).strip().splitlines()[0] if str(e.orig).strip() else type(e).__name__


def _retry(what: str, fn: Callable[[], T]) -> T | None:
    """Start-up only (schema, replay): wait for the database.
    Any other database error is a bad request: log it and return None."""
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


def _alert(dispatcher: Dispatcher, evs: Sequence[Event], refs: Sequence[object]) -> None:
    """Log every event; hand the ones that warrant a message to the notifiers."""
    for e, ref in zip(evs, refs):
        if e.notify:
            log.warning("ALERT  %s", e.message)
            dispatcher.submit(e, ref)
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
    cleared_nodes = store.manually_cleared_nodes()
    for n in store.all_nodes():
        d = engine.node(n.dev_eui, _label(cfg, n.dev_eui, n.name))
        if d.last_time is None:  # known node, silent for the whole replay window
            d.last_time = n.last_seen
        d.online = n.online
        d.last_contact = n.last_seen
        d.last_message_type = n.last_message_type
        if n.dev_eui in cleared_nodes:
            # Replayed pre-clear legacy readings must not resurrect an old alarm.
            # Persisted current state also includes any legitimate post-clear alert.
            d.state, d.state_since = n.state, n.state_since
            d.last_manual_clear = cleared_nodes[n.dev_eui]
            d._up = d._down = 0
            if n.state == 'NORMAL':
                d._last_notified = {}
                d._episode_notified = False
        if n.last_message_type in ("HEARTBEAT", "FIRE_ALERT"):
            d.state, d.state_since = n.state, n.state_since
        d.explicit_fire_active = bool(n.explicit_fire_active)
        if d.explicit_fire_active:
            d.state, d.state_since = "ALARM", n.state_since
    engine.control_keys = store.control_keys()
    store.save_node_states(engine.nodes.values())
    log.info("replayed %d readings from the last %gh: %s", len(rows),
             cfg.detection.replay_hours_on_startup,
             ", ".join(f"{d.name}={d.state}" for d in engine.nodes.values()) or "no nodes yet")


class Pipeline:
    """What the service does with each uplink and on each timer, without MQTT.
    Never touches the database itself: writes go to the writer's queue."""

    def __init__(self, cfg: Config, engine: DetectionEngine, tracker: clog.BlockTracker,
                 writer: Writer, dispatcher: Dispatcher,
                 gateways_seen: dict[str, datetime] | None = None):
        self.cfg = cfg
        self.engine = engine
        self.tracker = tracker
        self.writer = writer
        self.dispatcher = dispatcher
        self.gateways_seen = gateways_seen if gateways_seen is not None else {}
        self.pending_clears = {}
        self.deferred_uplinks = {}

    def request_clear(self, command, now):
        if time.monotonic() > command.deadline:
            command.result.set_result(unavailable())
            return
        d = self.engine.nodes.get(command.dev_eui)
        if d is None:
            command.result.set_result((404, {'detail': 'Unknown node'}))
            return
        if command.dev_eui in self.pending_clears:
            command.result.set_result((409, {'detail': 'Clear already in progress; refresh status.'}))
            return
        event = copy.copy(d).manual_clear(now)
        site = self.cfg.sites.node(command.dev_eui)
        job = ClearAlarmJob(command, event, site.block if site else None)
        self.pending_clears[command.dev_eui] = job
        self.deferred_uplinks[command.dev_eui] = []
        self.writer.submit(job)

    def finish_clears(self):
        # Main detection thread only. Serialize this node's uplinks behind the
        # transaction, while other nodes continue detecting and notifying.
        for node, job in list(self.pending_clears.items()):
            if not job.done.is_set():
                continue
            status, body = job.result
            if status == 200 and body.get('cleared'):
                self.engine.nodes[node].manual_clear(job.event.time)
                self.writer.submit(LogJob(self.tracker.update(self.engine.nodes, job.event.time)))
            elif status == 200 and body.get('state') == 'NORMAL' and body.get('state_since'):
                # Reconcile an earlier commit whose HTTP/DB acknowledgment was lost.
                self.engine.nodes[node].manual_clear(datetime.fromisoformat(body['state_since']))
            del self.pending_clears[node]
            for got, received_at in self.deferred_uplinks.pop(node):
                self.on_uplink(got, received_at)
            job.request.result.set_result((status, body))

    def on_uplink(self, got: Accepted, received_at: datetime) -> None:
        up, reading = got.uplink, got.reading
        if up.dev_eui in self.pending_clears:
            self.deferred_uplinks[up.dev_eui].append((got, received_at))
            return
        for g in up.gateways:
            last = self.gateways_seen.get(g.gateway_id)
            if last is None or up.time > last:
                self.gateways_seen[g.gateway_id] = up.time
        if isinstance(reading, ControlMessage):
            key = uplink_key(up)
            if key in self.engine.control_keys:
                return
            self.engine.control_keys.add(key)
            d = self.engine.node(up.dev_eui, _label(self.cfg, up.dev_eui, up.device_name))
            event = d.control(up.time, reading.kind)
            ref = EventRef()
            if event:
                event.details.update(uplink_key=key, f_cnt=up.f_cnt, f_port=up.f_port,
                                     received_at=received_at.isoformat())
            self.writer.submit(ControlJob(up, reading.kind, received_at, NodeState.of(d), event, ref))
            if event:
                _alert(self.dispatcher, [event], [ref])
                self.writer.submit(LogJob(clog.node_entries([event], self.cfg.sites)
                    + self.tracker.update(self.engine.nodes, up.time)))
            return
        ev = self.engine.process(up.dev_eui, _label(self.cfg, up.dev_eui, up.device_name),
                                 up.time, reading)
        log_reading(up, reading, "received")
        self.writer.submit(ReadingJob(up, reading, received_at, ev,
                                      NodeState.of(self.engine.nodes[up.dev_eui])))
        self._raise(ev.events, up.time)

    def check_offline(self, now: datetime) -> None:
        self._raise([e for key, d in self.engine.nodes.items()
                     if key not in self.pending_clears and (e := d.check_offline(now))], now)

    def system_check(self, now: datetime) -> None:
        """(Gateways heard within the missing threshold)/(configured or ever seen),
        from memory, so it works while the database is down."""
        cutoff = now - timedelta(minutes=(self.cfg.detection.explicit_missing_minutes
            if self.cfg.simulator.reporting_mode == "event_driven" else self.cfg.timing.missing_after_minutes))
        known = {g.gateway_id for g in self.cfg.sites.gateways} | set(self.gateways_seen)
        online = sum(t >= cutoff for t in self.gateways_seen.values())
        self.writer.submit(LogJob([clog.system_check(now, self.engine.nodes, online, len(known))]))

    def _raise(self, evs: Sequence[Event], t: datetime) -> None:
        """Store the events (queued), alert at once, log them and any change in
        the block picture."""
        if not evs:
            return
        refs = [EventRef() for _ in evs]
        states = {e.dev_eui: NodeState.of(self.engine.nodes[e.dev_eui]) for e in evs}
        self.writer.submit(EventsJob(evs, refs, states))
        _alert(self.dispatcher, evs, refs)
        entries = clog.node_entries(evs, self.cfg.sites) + self.tracker.update(self.engine.nodes, t)
        self.writer.submit(LogJob(entries))


def run(cfg: Config) -> int:
    command_settings = CommandSettings.from_env()
    try:
        notifiers = build_notifiers(cfg.notifiers)
    except NotifierConfigError as e:
        log.error("notifier setup: %s", e)
        return 1
    log.info("notifiers: %s", ", ".join(n.name for n in notifiers) or "none")

    # Start-up needs the database (schema, baselines). It waits for it.
    db = make_engine(cfg.database.url)
    log.info("database: %s", db.url.render_as_string(hide_password=True))
    _retry("schema setup", lambda: init_schema(db))
    store = Store(db)
    engine = DetectionEngine(cfg.detection)
    _retry("startup replay", lambda: _replay(cfg, store, engine))
    gateways_seen = {r.gateway_id: r.last_seen for r in _retry("reading gateways", store.all_gateways) or []}
    tracker = clog.BlockTracker(cfg.sites)
    tracker.prime(engine.nodes)

    # From here on, nothing waits for the database.
    writer = Writer(store, cfg.database.max_queued_writes).start()

    def on_result(e: Event, _ref: object, channel: str, ok: bool, detail: str) -> None:
        writer.submit(LogJob([clog.notification(utcnow(), headline(e), e.node_name, channel, ok,
                                                detail, e.dev_eui)]))

    dispatcher = Dispatcher(notifiers, on_delivered=lambda ref: writer.submit(NotifiedJob(ref)),
                            on_result=on_result).start()
    pipeline = Pipeline(cfg, engine, tracker, writer, dispatcher, gateways_seen)
    ingestor = Ingestor()

    # paho delivers messages on its own thread; process them here, one at a time.
    # paho acknowledges a QoS 1 message to the broker once it is in this inbox.
    inbox: queue.Queue[tuple[str, bytes, object]] = queue.Queue()
    try:
        client = connect(cfg.mqtt, cfg.mqtt.service_client_id, [cfg.mqtt.uplink_topic],
                         lambda msg: inbox.put((msg.topic, msg.payload, utcnow())), persistent=True)
    except BrokerUnavailable as e:
        log.error("%s", e)
        writer.stop(STOP_FLUSH_SECONDS)
        dispatcher.stop()
        return 1

    command_inbox = queue.Queue(maxsize=32)
    try:
        command_server = start_command_server(command_settings, command_inbox)
    except OSError:
        log.error('Operator command listener could not bind; detection continues with clearing disabled')
        command_server = None

    log.info("detection service running, listening on %s. Ctrl+C to stop.", cfg.mqtt.uplink_topic)
    writer.submit(LogJob([clog.service_started(
        utcnow(), len(engine.nodes), [n.name for n in notifiers], cfg.timing.demo_speedup)]))
    next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
    # Offline checks run often enough for a demo speed-up's short thresholds.
    offline_every = min(OFFLINE_CHECK_SECONDS, cfg.timing.heartbeat_minutes * 60 / 2)
    next_offline_check = time.monotonic() + offline_every
    system_check_every = cfg.timing.system_check_minutes * 60
    next_system_check = time.monotonic() + system_check_every
    try:
        while True:
            pipeline.finish_clears()
            try:
                command = command_inbox.get_nowait()
            except queue.Empty:
                pass
            else:
                pipeline.request_clear(command, utcnow())
            try:
                topic, body, received_at = inbox.get(timeout=1.0)
            except queue.Empty:
                pass
            else:
                got = ingestor.accept(topic, body, received_at)
                if got is not None:
                    pipeline.on_uplink(got, received_at)

            now_m = time.monotonic()
            if now_m >= next_offline_check:
                next_offline_check = now_m + offline_every
                pipeline.check_offline(utcnow())

            if now_m >= next_system_check:
                next_system_check = now_m + system_check_every
                pipeline.system_check(utcnow())

            if now_m >= next_heartbeat:
                next_heartbeat = now_m + HEARTBEAT_SECONDS
                s = ingestor.stats
                log.info("alive: %d accepted, %d duplicates, %d rejected, %d queued; "
                         "database %s, %d writes waiting, %d dropped; %s",
                         s.accepted, s.duplicates, s.rejected, inbox.qsize(),
                         "DOWN" if writer.database_down else "ok", writer.pending, writer.dropped,
                         ", ".join(f"{d.name}={d.state}" for d in engine.nodes.values()))
    except KeyboardInterrupt:
        log.info("stopping (sending any queued alerts and writes first)")
        return 0
    finally:
        if command_server is not None:
            command_server.shutdown()
            command_server.server_close()
        client.loop_stop()
        client.disconnect()
        dispatcher.stop()
        writer.submit(LogJob([clog.service_stopped(utcnow())]))
        writer.stop(STOP_FLUSH_SECONDS)


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
