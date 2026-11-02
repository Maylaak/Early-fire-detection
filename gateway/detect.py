"""Fire detection engine. Pure logic: no database, no MQTT, no wall clock.

Per node, per channel, an EWMA baseline tracks "normal" (temperature,
humidity, and gas resistance in log space). Each reading is compared with
the baseline as it was BEFORE that reading:

    gas_drop_pct        how far gas resistance is BELOW baseline, in %
                        (BME688 resistance FALLS when smoke/VOCs rise)
    temperature_rise_c  degC above baseline
    humidity_drop_pct   %RH points below baseline

A reading reaches a level (WATCH or ALARM) only when the channels agree:
    gas drop >= threshold  AND  (temperature rise >= threshold OR humidity drop >= threshold)

Nothing is compared with an absolute value, so slow sensor drift over weeks
is absorbed by the baseline.

States: WARMUP -> NORMAL <-> WATCH <-> ALARM. Moving up needs
confirm_samples.watch / .alarm consecutive readings at the higher level,
and NORMAL can only reach ALARM through WATCH. Moving down one level needs
confirm_samples.clear consecutive readings below the current state.

The baseline is NOT updated while the node is in a freeze state
(WATCH/ALARM by default) or while the reading itself is anomalous, so it
cannot chase the smoke. A safety valve resumes updates after
max_baseline_freeze_minutes, so a permanent sensor shift cannot leave a node
stuck in WATCH forever.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from gateway.codec import SensorReading
from gateway.config import DetectionConfig, LevelThresholds

WARMUP, NORMAL, WATCH, ALARM = "WARMUP", "NORMAL", "WATCH", "ALARM"
KIND_STATE, KIND_OFFLINE, KIND_ONLINE = "STATE", "OFFLINE", "ONLINE"

_RANK = {NORMAL: 0, WATCH: 1, ALARM: 2}
_BY_RANK = {rank: state for state, rank in _RANK.items()}


@dataclass(frozen=True)
class Baseline:
    temperature_c: float
    humidity_pct: float
    gas_ohm: float


@dataclass(frozen=True)
class Deviation:
    gas_drop_pct: float  # positive = resistance below baseline (the smoke direction)
    temperature_rise_c: float
    humidity_drop_pct: float

    def meets(self, th: LevelThresholds) -> bool:
        return self.gas_drop_pct >= th.gas_drop_pct and (
            self.temperature_rise_c >= th.temperature_rise_c
            or self.humidity_drop_pct >= th.humidity_drop_pct
        )

    def describe(self) -> str:
        return (f"gas {-self.gas_drop_pct:+.0f}% vs baseline, "
                f"T {self.temperature_rise_c:+.1f} C, RH {-self.humidity_drop_pct:+.1f} pts")


@dataclass(frozen=True)
class Event:
    dev_eui: str
    node_name: str
    time: datetime
    kind: str  # STATE, OFFLINE, ONLINE
    message: str
    notify: bool
    from_state: str | None = None
    to_state: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Evaluation:
    state: str  # node state after this reading
    evaluated: bool  # False during warm-up or for an out-of-order reading
    level: str | None = None  # what this reading alone indicates
    baseline: Baseline | None = None  # baseline the reading was compared with
    deviation: Deviation | None = None
    events: tuple[Event, ...] = ()


def ewma_alpha(dt_minutes: float, half_life_minutes: float) -> float:
    """Weight of a new sample arriving dt after the previous one."""
    return 1.0 - 0.5 ** (dt_minutes / half_life_minutes)


class NodeDetector:
    def __init__(self, dev_eui: str, name: str, cfg: DetectionConfig):
        self.dev_eui = dev_eui
        self.name = name
        self.cfg = cfg
        self.state = WARMUP
        self.state_since: datetime | None = None
        self.samples = 0
        self.last_time: datetime | None = None
        self.online = True
        self._temp = self._hum = self._log_gas = 0.0
        self._up = self._down = 0
        self._frozen_since: datetime | None = None
        self._last_notified: dict[str, datetime] = {}
        self._episode_notified = False

    @property
    def baseline(self) -> Baseline | None:
        if self.samples == 0:
            return None
        return Baseline(self._temp, self._hum, math.exp(self._log_gas))

    def process(self, t: datetime, r: SensorReading) -> Evaluation:
        if self.last_time is not None and t <= self.last_time:
            return Evaluation(self.state, evaluated=False)  # late or out of order: skip
        events: list[Event] = []
        if not self.online:
            self.online = True
            events.append(self._event(t, KIND_ONLINE, f"{self.name} is back online", notify=True))

        dt_min = 0.0 if self.last_time is None else (t - self.last_time).total_seconds() / 60
        self.last_time = t
        log_gas = math.log(max(r.gas_resistance_ohm, 1.0))

        if self.samples == 0:
            self.state_since = t
            self._update_baseline(dt_min, r, log_gas)
            return Evaluation(self.state, evaluated=False, events=tuple(events))

        before = self.baseline
        dev = Deviation(
            gas_drop_pct=(1.0 - math.exp(log_gas - self._log_gas)) * 100,
            temperature_rise_c=r.temperature_c - self._temp,
            humidity_drop_pct=self._hum - r.humidity_pct,
        )

        if self.state == WARMUP:
            self._update_baseline(dt_min, r, log_gas)
            if self.samples >= self.cfg.warmup_samples:
                events.append(self._transition(t, NORMAL, dev))
            return Evaluation(self.state, evaluated=False, baseline=before, deviation=dev,
                              events=tuple(events))

        level = ALARM if dev.meets(self.cfg.alarm) else WATCH if dev.meets(self.cfg.watch) else NORMAL
        new_state = self._hysteresis(level)
        if new_state != self.state:
            events.append(self._transition(t, new_state, dev))

        if level != NORMAL or self.state in self.cfg.freeze_baseline_states:
            if self._frozen_since is None:
                self._frozen_since = t
            max_freeze = timedelta(minutes=self.cfg.max_baseline_freeze_minutes)
            if t - self._frozen_since >= max_freeze:
                self._update_baseline(dt_min, r, log_gas)
        else:
            self._frozen_since = None
            self._update_baseline(dt_min, r, log_gas)

        return Evaluation(self.state, True, level, before, dev, tuple(events))

    def check_offline(self, now: datetime) -> Event | None:
        if not self.online or self.last_time is None:
            return None
        silent = now - self.last_time
        if silent <= timedelta(minutes=self.cfg.offline_after_minutes):
            return None
        self.online = False
        secs = silent.total_seconds()
        quiet = f"{secs:.0f} s" if secs < 120 else f"{secs / 60:.0f} min"  # seconds: demo speed-up
        return self._event(
            now, KIND_OFFLINE,
            f"{self.name} OFFLINE: no uplink for {quiet} (last state {self.state})",
            notify=True,
        )

    def _update_baseline(self, dt_min: float, r: SensorReading, log_gas: float) -> None:
        n = self.samples + 1
        hl = self.cfg.baseline_half_life_minutes

        # 1/n during warm-up gives a plain mean of the first samples, then the
        # time-based EWMA weight takes over. dt-based, so gaps are handled.
        def alpha(half_life: float) -> float:
            return max(ewma_alpha(dt_min, half_life), 1.0 / n)

        self._temp += alpha(hl.temperature) * (r.temperature_c - self._temp)
        self._hum += alpha(hl.humidity) * (r.humidity_pct - self._hum)
        self._log_gas += alpha(hl.gas) * (log_gas - self._log_gas)
        self.samples = n

    def _hysteresis(self, level: str) -> str:
        cur, lvl, c = _RANK[self.state], _RANK[level], self.cfg.confirm_samples
        if lvl > cur:
            self._up, self._down = self._up + 1, 0
            if self._up >= (c.watch if cur == _RANK[NORMAL] else c.alarm):
                self._up = 0
                return _BY_RANK[cur + 1]  # one level at a time: NORMAL -> WATCH -> ALARM
        elif lvl < cur:
            self._down, self._up = self._down + 1, 0
            if self._down >= c.clear:
                self._down = 0
                return _BY_RANK[cur - 1]
        else:
            self._up = self._down = 0
        return self.state

    def _transition(self, t: datetime, new: str, dev: Deviation) -> Event:
        old = self.state
        self.state, self.state_since = new, t
        notify = False
        if new == ALARM:
            notify = not self._notified_within(t, ALARM)
        elif new == WATCH and old == NORMAL:
            notify = not self._notified_within(t, WATCH, ALARM)
        elif new == NORMAL and old != WARMUP:
            notify = self._episode_notified  # all-clear only if we raised something
            self._episode_notified = False
        if notify and new != NORMAL:
            self._last_notified[new] = t
            self._episode_notified = True

        label = {ALARM: "FIRE ALARM", WATCH: "WATCH", NORMAL: "back to NORMAL"}.get(new, new)
        if old == WARMUP:
            label = "warm-up complete, monitoring"
        b = self.baseline
        return self._event(
            t, KIND_STATE, f"{self.name}: {label} ({old} -> {new}); {dev.describe()}",
            notify=notify, from_state=old, to_state=new,
            details={
                "gas_drop_pct": round(dev.gas_drop_pct, 2),
                "temperature_rise_c": round(dev.temperature_rise_c, 2),
                "humidity_drop_pct": round(dev.humidity_drop_pct, 2),
                "baseline_temperature_c": round(b.temperature_c, 2),
                "baseline_humidity_pct": round(b.humidity_pct, 2),
                "baseline_gas_ohm": round(b.gas_ohm),
            },
        )

    def _notified_within(self, t: datetime, *states: str) -> bool:
        window = timedelta(minutes=self.cfg.notify_cooldown_minutes)
        return any(s in self._last_notified and t - self._last_notified[s] < window for s in states)

    def _event(self, t: datetime, kind: str, message: str, notify: bool, **kw: Any) -> Event:
        return Event(self.dev_eui, self.name, t, kind, message, notify, **kw)


class DetectionEngine:
    """One NodeDetector per node, created on first sight."""

    def __init__(self, cfg: DetectionConfig):
        self.cfg = cfg
        self.nodes: dict[str, NodeDetector] = {}

    def node(self, dev_eui: str, name: str) -> NodeDetector:
        d = self.nodes.get(dev_eui)
        if d is None:
            d = self.nodes[dev_eui] = NodeDetector(dev_eui, name, self.cfg)
        d.name = name
        return d

    def process(self, dev_eui: str, name: str, t: datetime, reading: SensorReading) -> Evaluation:
        return self.node(dev_eui, name).process(t, reading)

    def check_offline(self, now: datetime) -> list[Event]:
        return [e for d in self.nodes.values() if (e := d.check_offline(now)) is not None]
