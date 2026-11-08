"""Ingestion: MQTT message -> ChirpStack envelope -> payload decode -> duplicate check.

MQTT QoS 1 delivers at least once, so the same uplink can arrive twice (and
again after a restart, from the broker's queue). `accept` drops a repeat
before it can reach detection, so it is never counted twice in a baseline.
The database's unique uplink_key is the last line of defence.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from gateway.chirpstack import EnvelopeError, Uplink, parse_uplink
from gateway.codec import CodecError, SensorReading, decode
from gateway.store import Store, uplink_key

log = logging.getLogger(__name__)

RECENT_KEYS_PER_NODE = 256  # far more than a redelivery burst after a reconnect


@dataclass(frozen=True)
class Accepted:
    uplink: Uplink
    reading: SensorReading


@dataclass(frozen=True)
class Ingested:
    reading_id: int
    uplink: Uplink
    reading: SensorReading


@dataclass
class IngestStats:
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0


class Ingestor:
    def __init__(self, store: Store | None = None):
        self.store = store  # only handle() writes
        self.stats = IngestStats()
        self._recent: dict[str, tuple[deque[str], set[str]]] = {}

    def accept(self, topic: str, body: bytes, received_at: datetime) -> Accepted | None:
        """Parse, decode and de-duplicate one MQTT message, in memory only.
        Bad messages and repeats are logged and skipped, never raised: one
        broken node must not stop ingestion for the others."""
        try:
            up = parse_uplink(topic, body, received_at)
            reading = decode(up.data)
        except (EnvelopeError, CodecError) as e:
            self.stats.rejected += 1
            log.warning("rejected message on %s: %s", topic, e)
            return None
        if not self._first_time(up):
            self.stats.duplicates += 1
            log.info("duplicate uplink from %s fCnt=%d ignored", up.device_name, up.f_cnt)
            return None
        self.stats.accepted += 1
        return Accepted(up, reading)

    def _first_time(self, up: Uplink) -> bool:
        order, seen = self._recent.setdefault(up.dev_eui, (deque(), set()))
        key = uplink_key(up)
        if key in seen:
            return False
        order.append(key)
        seen.add(key)
        if len(order) > RECENT_KEYS_PER_NODE:
            seen.discard(order.popleft())
        return True

    def handle(self, topic: str, body: bytes, received_at: datetime) -> Ingested | None:
        """accept() and store the reading straight away, without detection
        (tests and tools; the detection service stores through its writer).
        Database errors propagate."""
        got = self.accept(topic, body, received_at)
        if got is None:
            return None
        up, r = got.uplink, got.reading
        reading_id = self.store.save_uplink(up, r, received_at)
        if reading_id is None:  # already in the database (e.g. before a restart)
            self.stats.accepted -= 1
            self.stats.duplicates += 1
            log.info("duplicate uplink from %s fCnt=%d ignored", up.device_name, up.f_cnt)
            return None
        log_reading(up, r, "stored")
        return Ingested(reading_id, up, r)


def log_reading(up: Uplink, r: SensorReading, what: str) -> None:
    log.info(
        "%s %-7s fCnt=%-5d T=%6.2fC RH=%5.1f%% gas=%7.1fkOhm batt=%.2fV rssi=%s snr=%s%s",
        what, up.device_name, up.f_cnt, r.temperature_c, r.humidity_pct,
        r.gas_resistance_ohm / 1000, r.battery_v, up.rssi, up.snr,
        " GAS SATURATED" if r.gas_saturated else "",
    )
