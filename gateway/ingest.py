"""Ingestion: MQTT message -> ChirpStack envelope -> payload decode -> database."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from gateway.chirpstack import EnvelopeError, Uplink, parse_uplink
from gateway.codec import CodecError, SensorReading, decode
from gateway.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Ingested:
    reading_id: int
    uplink: Uplink
    reading: SensorReading


@dataclass
class IngestStats:
    stored: int = 0
    duplicates: int = 0
    rejected: int = 0


class Ingestor:
    def __init__(self, store: Store):
        self.store = store
        self.stats = IngestStats()

    def handle(self, topic: str, body: bytes, received_at: datetime) -> Ingested | None:
        """Process one MQTT message. Bad messages are logged and skipped, never
        raised: one broken node must not stop ingestion for the others.
        Database errors DO propagate, so the caller can retry."""
        try:
            up = parse_uplink(topic, body, received_at)
            reading = decode(up.data)
        except (EnvelopeError, CodecError) as e:
            self.stats.rejected += 1
            log.warning("rejected message on %s: %s", topic, e)
            return None

        reading_id = self.store.save_uplink(up, reading, received_at)
        if reading_id is None:
            self.stats.duplicates += 1
            log.info("duplicate uplink from %s fCnt=%d ignored", up.device_name, up.f_cnt)
            return None

        self.stats.stored += 1
        r = reading
        log.info(
            "stored %-7s fCnt=%-5d T=%6.2fC RH=%5.1f%% gas=%7.1fkOhm batt=%.2fV rssi=%s snr=%s%s",
            up.device_name, up.f_cnt, r.temperature_c, r.humidity_pct,
            r.gas_resistance_ohm / 1000, r.battery_v, up.rssi, up.snr,
            " GAS SATURATED" if r.gas_saturated else "",
        )
        return Ingested(reading_id, up, reading)
