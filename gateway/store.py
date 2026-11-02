"""Writes from the detection service to the database."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import Connection, Engine, Row, insert, select, update
from sqlalchemy.exc import IntegrityError

from gateway.chirpstack import Uplink
from gateway.codec import SensorReading
from gateway.console_log import LogEntry
from gateway.db import console_log, events, gateways, nodes, readings
from gateway.detect import Evaluation, Event, NodeDetector


def uplink_key(up: Uplink) -> str:
    """ChirpStack's deduplicationId if present, else device + counter + time."""
    return up.deduplication_id or f"{up.dev_eui}:{up.f_cnt}:{up.time.isoformat()}"


class Store:
    def __init__(self, engine: Engine):
        self.engine = engine

    def save_uplink(self, up: Uplink, reading: SensorReading, received_at: datetime) -> int | None:
        """Store one decoded uplink. Returns the new reading id, or None if it
        was a duplicate (same uplink delivered twice)."""
        try:
            with self.engine.begin() as conn:
                self._upsert_node(conn, up, reading)
                self._upsert_gateways(conn, up)
                result = conn.execute(
                    insert(readings).values(
                        dev_eui=up.dev_eui,
                        time=up.time,
                        received_at=received_at,
                        uplink_key=uplink_key(up),
                        f_cnt=up.f_cnt,
                        f_port=up.f_port,
                        temperature_c=reading.temperature_c,
                        humidity_pct=reading.humidity_pct,
                        pressure_hpa=reading.pressure_hpa,
                        gas_resistance_ohm=reading.gas_resistance_ohm,
                        gas_saturated=reading.gas_saturated,
                        battery_v=reading.battery_v,
                        rssi=up.rssi,
                        snr=up.snr,
                        gateway_count=up.gateway_count,
                    )
                )
                return result.inserted_primary_key[0]
        except IntegrityError:
            # Only the unique uplink_key can fail here: the node row exists by now.
            return None

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
                conn.execute(update(readings).where(readings.c.id == reading_id).values(
                    baseline_temperature_c=ev.baseline.temperature_c,
                    baseline_humidity_pct=ev.baseline.humidity_pct,
                    baseline_gas_ohm=ev.baseline.gas_ohm,
                    state=ev.state,
                ))
            self._write_node_state(conn, node)
            return self._insert_events(conn, ev.events)

    def save_events(self, evs: Sequence[Event], detectors: dict[str, NodeDetector]) -> list[int]:
        """Events not tied to a reading (OFFLINE), plus the affected nodes' state."""
        with self.engine.begin() as conn:
            for dev_eui in {e.dev_eui for e in evs}:
                self._write_node_state(conn, detectors[dev_eui])
            return self._insert_events(conn, evs)

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
    def _write_node_state(conn: Connection, d: NodeDetector) -> None:
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
