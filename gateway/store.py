"""Writes from the detection service to the database."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Connection, Engine, Row, insert, select, update
from sqlalchemy.exc import IntegrityError

from gateway.chirpstack import Uplink
from gateway.codec import SensorReading
from gateway.console_log import LogEntry
from gateway.db import console_log, events, gateways, nodes, readings
from gateway.detect import Evaluation, Event, NodeDetector


def uplink_key(up: Uplink) -> str:
    """What makes an uplink the same uplink: node + frame counter + uplink time.

    A QoS 1 redelivery is the same message, so all three match. The frame
    counter alone is not enough: it restarts at 0 when a node rejoins or
    reboots, and the next real readings must not be taken for old ones. The
    time tells them apart. (ChirpStack's deduplicationId is not used: it is
    specific to ChirpStack, and a different id for the same node, counter and
    time would still be the same reading.)"""
    return f"{up.dev_eui}:{up.f_cnt}:{up.time.astimezone(timezone.utc).isoformat()}"


@dataclass(frozen=True)
class NodeState:
    """A node's detector state at one moment, for writing later (the detector
    itself keeps changing while a write waits for the database)."""

    dev_eui: str
    state: str
    state_since: datetime | None
    online: bool

    @classmethod
    def of(cls, d: NodeDetector) -> NodeState:
        return cls(d.dev_eui, d.state, d.state_since, d.online)


class Store:
    def __init__(self, engine: Engine):
        self.engine = engine

    def save_reading(self, up: Uplink, reading: SensorReading, received_at: datetime,
                     ev: Evaluation, node: NodeState) -> int | None:
        """One uplink with its detection result, in one transaction: the
        reading (with the baseline it was compared with), the node's latest
        values and state, the gateways. None if it is already stored."""
        try:
            with self.engine.begin() as conn:
                self._upsert_node(conn, up, reading)
                self._upsert_gateways(conn, up)
                result = conn.execute(insert(readings).values(
                    **self._reading_values(up, reading, received_at), **self._evaluation_values(ev)))
                self._write_node_state(conn, node)
                return result.inserted_primary_key[0]
        except IntegrityError:
            return None

    def save_uplink(self, up: Uplink, reading: SensorReading, received_at: datetime) -> int | None:
        """Store one decoded uplink. Returns the new reading id, or None if it
        was a duplicate (same uplink delivered twice)."""
        try:
            with self.engine.begin() as conn:
                self._upsert_node(conn, up, reading)
                self._upsert_gateways(conn, up)
                result = conn.execute(insert(readings).values(**self._reading_values(up, reading, received_at)))
                return result.inserted_primary_key[0]
        except IntegrityError:
            # Only the unique uplink_key can fail here: the node row exists by now.
            return None

    @staticmethod
    def _reading_values(up: Uplink, reading: SensorReading, received_at: datetime) -> dict:
        return {
            "dev_eui": up.dev_eui, "time": up.time, "received_at": received_at,
            "uplink_key": uplink_key(up), "f_cnt": up.f_cnt, "f_port": up.f_port,
            "temperature_c": reading.temperature_c, "humidity_pct": reading.humidity_pct,
            "pressure_hpa": reading.pressure_hpa, "gas_resistance_ohm": reading.gas_resistance_ohm,
            "gas_saturated": reading.gas_saturated, "battery_v": reading.battery_v,
            "rssi": up.rssi, "snr": up.snr, "gateway_count": up.gateway_count,
        }

    @staticmethod
    def _evaluation_values(ev: Evaluation) -> dict:
        if ev.baseline is None:  # the node's first reading has nothing to compare with
            return {}
        return {"baseline_temperature_c": ev.baseline.temperature_c,
                "baseline_humidity_pct": ev.baseline.humidity_pct,
                "baseline_gas_ohm": ev.baseline.gas_ohm, "state": ev.state}

    def _upsert_node(self, conn: Connection, up: Uplink, reading: SensorReading) -> None:
        row = conn.execute(
            select(nodes.c.last_seen).where(nodes.c.dev_eui == up.dev_eui)
        ).first()
        latest = {
            "name": up.device_name,
            "application_name": up.application_name,
            "last_seen": up.time,
            "last_f_cnt": up.f_cnt,
            "last_rssi": up.rssi,
            "last_snr": up.snr,
            "battery_v": reading.battery_v,
        }
        if row is None:
            conn.execute(insert(nodes).values(dev_eui=up.dev_eui, first_seen=up.time, **latest))
        elif up.time >= row.last_seen:
            # A late or redelivered uplink must not move last_seen backwards.
            conn.execute(update(nodes).where(nodes.c.dev_eui == up.dev_eui).values(**latest))

    @staticmethod
    def _upsert_gateways(conn: Connection, up: Uplink) -> None:
        for gw in up.gateways:
            row = conn.execute(
                select(gateways.c.last_seen).where(gateways.c.gateway_id == gw.gateway_id)
            ).first()
            if row is None:
                conn.execute(insert(gateways).values(
                    gateway_id=gw.gateway_id, first_seen=up.time, last_seen=up.time,
                    last_rssi=gw.rssi, last_snr=gw.snr, uplinks=1))
                continue
            latest = {"uplinks": gateways.c.uplinks + 1}
            if up.time >= row.last_seen:
                latest.update(last_seen=up.time, last_rssi=gw.rssi, last_snr=gw.snr)
            conn.execute(update(gateways).where(gateways.c.gateway_id == gw.gateway_id).values(**latest))

    def all_gateways(self) -> list[Row]:
        with self.engine.connect() as conn:
            return conn.execute(select(gateways)).all()

    # --- operator console log --------------------------------------------------

    def append_log(self, entries: Sequence[LogEntry]) -> None:
        if entries:
            with self.engine.begin() as conn:
                conn.execute(insert(console_log), [
                    {"time": e.time, "level": e.level, "category": e.category,
                     "message": e.message, "dev_eui": e.dev_eui, "block": e.block}
                    for e in entries])

    # --- detection results ---------------------------------------------------

    def save_evaluation(self, reading_id: int, ev: Evaluation, node: NodeDetector) -> list[int]:
        """Record the baseline/state on the reading, the node's state, and any
        events. One transaction. Returns the new event ids (same order as ev.events)."""
        with self.engine.begin() as conn:
            if ev.baseline is not None:
                conn.execute(update(readings).where(readings.c.id == reading_id)
                             .values(**self._evaluation_values(ev)))
            self._write_node_state(conn, node)
            return self._insert_events(conn, ev.events)

    def save_events(self, evs: Sequence[Event],
                    detectors: Mapping[str, NodeDetector | NodeState]) -> list[int]:
        """Events, plus the affected nodes' state. A node row is created if
        none exists yet (its first reading may still be waiting to be written,
        or was dropped from a full write queue)."""
        with self.engine.begin() as conn:
            for e in {e.dev_eui: e for e in evs}.values():
                self._ensure_node(conn, e.dev_eui, e.node_name, e.time)
                self._write_node_state(conn, detectors[e.dev_eui])
            return self._insert_events(conn, evs)

    @staticmethod
    def _ensure_node(conn: Connection, dev_eui: str, name: str, t: datetime) -> None:
        if conn.execute(select(nodes.c.dev_eui).where(nodes.c.dev_eui == dev_eui)).first() is None:
            conn.execute(insert(nodes).values(dev_eui=dev_eui, name=name, first_seen=t, last_seen=t))

    def save_node_states(self, detectors: Iterable[NodeDetector]) -> None:
        with self.engine.begin() as conn:
            for d in detectors:
                self._write_node_state(conn, d)

    def mark_notified(self, event_ids: Sequence[int]) -> None:
        if event_ids:
            with self.engine.begin() as conn:
                conn.execute(update(events).where(events.c.id.in_(event_ids)).values(notified=True))

    def readings_since(self, since: datetime) -> list[Row]:
        """For rebuilding baselines at startup, oldest first."""
        with self.engine.connect() as conn:
            return conn.execute(
                select(readings, nodes.c.name)
                .join(nodes, nodes.c.dev_eui == readings.c.dev_eui)
                .where(readings.c.time >= since)
                .order_by(readings.c.time, readings.c.id)
            ).all()

    def all_nodes(self) -> list[Row]:
        with self.engine.connect() as conn:
            return conn.execute(select(nodes)).all()

    @staticmethod
    def _write_node_state(conn: Connection, d: NodeDetector | NodeState) -> None:
        conn.execute(update(nodes).where(nodes.c.dev_eui == d.dev_eui).values(
            state=d.state, state_since=d.state_since, online=d.online))

    @staticmethod
    def _insert_events(conn: Connection, evs: Sequence[Event]) -> list[int]:
        ids = []
        for e in evs:
            result = conn.execute(insert(events).values(
                dev_eui=e.dev_eui, time=e.time, kind=e.kind, from_state=e.from_state,
                to_state=e.to_state, message=e.message, details=e.details, notified=False))
            ids.append(result.inserted_primary_key[0])
        return ids
