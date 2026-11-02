"""PLACEHOLDER node firmware: decides when a simulated node transmits.

The real node firmware is not written yet. This stands in for it so the
gateway sees a realistic uplink pattern instead of one reading per minute:

  - The node reads its sensor every firmware.sample_seconds.
  - It keeps its own slow gas baseline (EWMA, never frozen). When gas resistance is
    local_trigger.gas_drop_pct below it for confirm_samples reads in a row,
    the node enters ALARM MODE and transmits at once.
  - In alarm mode it transmits every alarm_interval_minutes, until
    alarm_hold_minutes after the trigger last held.
  - Otherwise it transmits a heartbeat every timing.heartbeat_minutes.
  - Alarm-mode uplinks go on lorawan.alarm_mode_f_port, heartbeats on
    lorawan.heartbeat_f_port (see config.yaml): that is how the gateway
    knows the mode. The real firmware must do the same.
  - It never transmits sooner than the EU868 duty cycle allows at its
    spreading factor; a due uplink waits for the first sensor read after the
    gap. (SF11: ~82 s, SF12: ~147 s for this payload, so slow nodes cannot
    keep a 1-minute alarm rate.)

The local trigger only decides the transmit rate. Fire detection is the
gateway's job (gateway/detect.py); a node in alarm mode is not an alarm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from gateway.chirpstack import duty_cycle_gap_seconds
from gateway.codec import PAYLOAD_LENGTH
from gateway.config import SimFirmware
from gateway.detect import ewma_alpha
from gateway.sim.model import NodeModel, Sample

HEARTBEAT, ALARM_MODE = "heartbeat", "alarm-mode"


@dataclass(frozen=True)
class Transmission:
    sample: Sample  # f_cnt is the uplink frame counter
    reason: str  # HEARTBEAT or ALARM_MODE


class NodeFirmware:
    def __init__(self, model: NodeModel, fw: SimFirmware, heartbeat_minutes: float,
                 speedup: float = 1.0):
        self.model = model
        self.fw = fw
        self.heartbeat = timedelta(minutes=heartbeat_minutes)
        # Airtime is physics, not config, so the demo speed-up is applied here.
        self.min_gap = timedelta(seconds=duty_cycle_gap_seconds(
            model.spreading_factor, PAYLOAD_LENGTH, fw.duty_cycle) / speedup)
        self._f_cnt = 0
        self._last_read: datetime | None = None
        self._last_tx: datetime | None = None
        self._log_gas = 0.0
        self._reads = 0
        self._hits = 0
        self._alarm_until: datetime | None = None
        self.triggers = 0  # times the local trigger fired (for measurement)

    @property
    def interval(self) -> timedelta:
        """How often the caller should call step()."""
        return timedelta(seconds=self.fw.sample_seconds)

    def in_alarm_mode(self, t: datetime) -> bool:
        return self._alarm_until is not None and t < self._alarm_until

    def step(self, t: datetime) -> Transmission | None:
        """One sensor read at time t. Returns the uplink, if the node sends one."""
        s = self.model.sample(t)
        fired = self._local_trigger(t, s.reading.gas_resistance_ohm)
        if fired:
            self._alarm_until = t + timedelta(minutes=self.fw.alarm_hold_minutes)

        alarm = self.in_alarm_mode(t)
        if self._last_tx is None:
            due = True
        else:
            since = t - self._last_tx
            due = fired or since >= (
                timedelta(minutes=self.fw.alarm_interval_minutes) if alarm else self.heartbeat)
            if since < self.min_gap:
                due = False  # duty cycle: must wait, whatever the reason
        if not due:
            return None
        self._last_tx = t
        f_cnt, self._f_cnt = self._f_cnt, self._f_cnt + 1
        return Transmission(replace(s, f_cnt=f_cnt), ALARM_MODE if alarm else HEARTBEAT)

    def _local_trigger(self, t: datetime, gas_ohm: float) -> bool:
        """True on the read where the trigger newly fires (or keeps firing)."""
        lt = self.fw.local_trigger
        log_gas = math.log(max(gas_ohm, 1.0))
        dt_min = 0.0 if self._last_read is None else (t - self._last_read).total_seconds() / 60
        self._last_read = t
        hit = False
        if self._reads >= lt.warmup_samples:
            drop_pct = (1.0 - math.exp(log_gas - self._log_gas)) * 100
            hit = drop_pct >= lt.gas_drop_pct
        self._hits = self._hits + 1 if hit else 0
        # Same EWMA as the gateway (1/n during warm-up). Never frozen: a node
        # with a permanent sensor shift must leave alarm mode by itself, or
        # its battery drains. With a 60 min half-life a 25 min smoke event
        # still reads ~50 % below baseline at its end.
        self._reads += 1
        a = max(ewma_alpha(dt_min, lt.baseline_half_life_minutes), 1.0 / self._reads)
        self._log_gas += a * (log_gas - self._log_gas)
        fired = self._hits >= lt.confirm_samples
        if fired and self._hits == lt.confirm_samples:
            self.triggers += 1
        return fired
