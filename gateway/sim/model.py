"""Physical model of one BME688 node.

Each reading is: per-node baseline
  + slow sensor/weather drift (random walk)
  + day/night cycle (warmer and drier in the afternoon)
  + occasional rain/fog (humidity up, temperature down)
  + noise and rare one-off gas glitches
  + an injected smoke event, if any.

Gas resistance is modelled in log space (its noise and drift are
multiplicative) and FALLS when humidity rises or smoke/VOCs rise.

The model has no clock of its own: the caller passes the time of each sample.
The live simulator passes the wall clock; tests can simulate weeks in seconds.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from gateway.codec import SensorReading
from gateway.config import SimNode, SimulatorConfig

# Lowest SNR each spreading factor can demodulate (Semtech SX127x datasheet).
SF_DEMOD_FLOOR_DB = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}
ADR_MARGIN_DB = 10.0


def adr_spreading_factor(snr_db: float) -> int:
    """Fastest SF that leaves ADR's ~10 dB link margin, like ChirpStack ADR would pick."""
    for sf, floor in SF_DEMOD_FLOOR_DB.items():
        if snr_db - floor >= ADR_MARGIN_DB:
            return sf
    return 12


@dataclass(frozen=True)
class Sample:
    time: datetime
    f_cnt: int
    reading: SensorReading
    rssi: int
    snr: float
    smoke_level: float  # 0..1, for the console log only; never transmitted
    raining: bool


@dataclass(frozen=True)
class _Event:
    """Trapezoid: ramp up, hold at full strength, ramp down."""

    start: datetime
    ramp: timedelta
    hold: timedelta

    @property
    def end(self) -> datetime:
        return self.start + 2 * self.ramp + self.hold

    def level(self, t: datetime) -> float:
        dt = t - self.start
        if dt <= timedelta(0) or t >= self.end:
            return 0.0
        if dt < self.ramp:
            return dt / self.ramp
        if dt < self.ramp + self.hold:
            return 1.0
        return 1.0 - (dt - self.ramp - self.hold) / self.ramp


class NodeModel:
    def __init__(self, node: SimNode, cfg: SimulatorConfig, rng: random.Random, start: datetime):
        self.node = node
        self.cfg = cfg
        self.rng = rng
        self.spreading_factor = adr_spreading_factor(node.snr_db)
        self._start = start
        self._last: datetime | None = None
        self._f_cnt = 0
        self._drift_t = 0.0
        self._drift_h = 0.0
        self._drift_p = 0.0
        self._drift_log_g = 0.0
        self._smoke: _Event | None = None
        self._rain: _Event | None = None

    def inject_smoke(
        self, at: datetime, hold_minutes: float | None = None, ramp_minutes: float | None = None
    ) -> None:
        s = self.cfg.smoke
        hold = s.hold_minutes if hold_minutes is None else hold_minutes
        ramp = s.ramp_minutes if ramp_minutes is None else ramp_minutes
        if ramp <= 0 or hold < 0:
            raise ValueError("ramp must be > 0 and hold >= 0 minutes")
        self._smoke = _Event(at, timedelta(minutes=ramp), timedelta(minutes=hold))

    def smoke_active(self, t: datetime) -> bool:
        return self._smoke is not None and t < self._smoke.end

    def sample(self, t: datetime) -> Sample:
        dt_h = 0.0 if self._last is None else max((t - self._last).total_seconds() / 3600, 0.0)
        self._last = t
        self._advance_drift(dt_h)
        self._maybe_start_rain(t, dt_h)

        rng, cfg, node, noise = self.rng, self.cfg, self.node, self.cfg.noise
        day = self._diurnal(t)
        rain = self._rain.level(t) if self._rain else 0.0
        smoke = self._smoke.level(t) if self._smoke else 0.0
        w, s = cfg.weather, cfg.smoke

        temperature = (
            node.temperature_c
            + self._drift_t
            + cfg.diurnal.temperature_amplitude_c * day
            - w.rain_temperature_drop_c * rain
            + s.temperature_rise_c * smoke
            + rng.gauss(0, noise.temperature_c)
        )
        humidity = (
            node.humidity_pct
            + self._drift_h
            - cfg.diurnal.humidity_amplitude_pct * day
            + w.rain_humidity_rise_pct * rain
            - s.humidity_drop_pct * smoke
            + rng.gauss(0, noise.humidity_pct)
        )
        humidity = min(max(humidity, 0.0), 100.0)
        pressure = node.pressure_hpa + self._drift_p + rng.gauss(0, noise.pressure_hpa)

        # Smoke is a DROP in resistance; so is a humidity rise.
        log_gas = (
            math.log(node.gas_resistance_ohm)
            + self._drift_log_g
            - cfg.diurnal.gas_humidity_coeff * (humidity - node.humidity_pct)
            + math.log(1.0 - s.gas_drop_fraction * smoke)
            + rng.gauss(0, noise.gas_fraction)
        )
        if rng.random() < noise.glitch_probability:
            log_gas += math.log(rng.uniform(0.3, 0.7))

        days = (t - self._start).total_seconds() / 86400
        battery = max(node.battery_v - cfg.battery_drain_v_per_day * days, 0.0)

        f_cnt = self._f_cnt
        self._f_cnt += 1
        return Sample(
            time=t,
            f_cnt=f_cnt,
            reading=SensorReading(
                temperature_c=temperature,
                humidity_pct=humidity,
                pressure_hpa=pressure,
                gas_resistance_ohm=math.exp(log_gas),
                battery_v=battery,
            ),
            rssi=round(node.rssi_dbm + rng.gauss(0, noise.rssi_db)),
            snr=round(node.snr_db + rng.gauss(0, noise.snr_db), 1),
            smoke_level=smoke,
            raining=rain > 0,
        )

    def _advance_drift(self, dt_h: float) -> None:
        if dt_h == 0:
            return
        d, rng, k = self.cfg.drift, self.rng, math.sqrt(dt_h)
        self._drift_t += rng.gauss(0, d.temperature_c * k)
        self._drift_h += rng.gauss(0, d.humidity_pct * k)
        self._drift_p += rng.gauss(0, d.pressure_hpa * k)
        self._drift_log_g += rng.gauss(0, d.gas_fraction * k)

    def _maybe_start_rain(self, t: datetime, dt_h: float) -> None:
        if self._rain is not None and t < self._rain.end:
            return
        w = self.cfg.weather
        if self.rng.random() < w.rain_events_per_day * dt_h / 24:
            self._rain = _Event(t, timedelta(hours=w.rain_ramp_hours), timedelta(hours=w.rain_hold_hours))

    def _diurnal(self, t: datetime) -> float:
        """+1 at the afternoon peak, -1 before dawn."""
        hour = t.hour + t.minute / 60 + t.second / 3600
        return math.cos(2 * math.pi * (hour - self.cfg.diurnal.peak_hour_utc) / 24)
