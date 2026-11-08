"""Detection engine.

The two that matter most:
  test_injected_smoke_triggers_watch_then_alarm
  test_long_normal_operation_never_alarms   <- the more important one

The rest pin down individual rules with small synthetic streams.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from gateway.codec import SensorReading
from gateway.config import DEFAULT_CONFIG_PATH, load_config
from gateway.detect import (
    ALARM,
    KIND_OFFLINE,
    KIND_ONLINE,
    NORMAL,
    WARMUP,
    WATCH,
    DetectionEngine,
    NodeDetector,
    ewma_alpha,
)
from gateway.sim.firmware import NodeFirmware
from gateway.sim.model import NodeModel

CFG = load_config(DEFAULT_CONFIG_PATH, env={})
SIM, DET = CFG.simulator, CFG.detection
T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
STEP = timedelta(seconds=60)
CONFIRM = DET.confirm_samples


def det(cfg=DET) -> NodeDetector:
    return NodeDetector("70b3d57ed0000001", "node-1", cfg)


def reading(temp=20.0, hum=50.0, gas=200_000.0) -> SensorReading:
    return SensorReading(temp, hum, 1000.0, gas, 3.9)


class Clock:
    def __init__(self, start=T0, step=STEP):
        self.t, self.step = start, step

    def __call__(self):
        self.t += self.step
        return self.t


def warm(d: NodeDetector, clock: Clock, **base) -> None:
    for _ in range(DET.warmup_samples):
        d.process(clock(), reading(**base))
    assert d.state == NORMAL


def feed(d, clock, n, **values):
    return [d.process(clock(), reading(**values)) for _ in range(n)]


def transitions(evals):
    return [(e.from_state, e.to_state) for ev in evals for e in ev.events if e.to_state]


SMOKE = dict(temp=26.0, hum=35.0, gas=80_000.0)  # vs base 20 C / 50 % / 200 kOhm


# --- the two required behaviours, on the realistic simulator -----------------

def simulate(node, seed, minutes, inject_at=None, sim=SIM):
    m = NodeModel(node, sim, random.Random(seed), T0)
    d = NodeDetector(node.dev_eui, node.name, DET)
    if inject_at is not None:
        m.inject_smoke(inject_at)
    events = []
    for i in range(minutes):
        t = T0 + i * STEP
        events.extend(d.process(t, m.sample(t).reading).events)
    return events


@pytest.mark.parametrize("hour", [0, 4, 8, 12, 16, 20])
@pytest.mark.parametrize("seed", [0, 1])
def test_injected_smoke_triggers_watch_then_alarm(hour, seed):
    node = SIM.nodes[seed]
    inject = T0 + timedelta(days=2, hours=hour)
    minutes = int((inject - T0) / STEP) + 30
    events = [e for e in simulate(node, seed, minutes, inject) if e.time >= inject]

    states = [e.to_state for e in events]
    assert states[:2] == [WATCH, ALARM], states
    alarm = events[1]
    assert alarm.time - inject <= timedelta(minutes=15)
    assert alarm.notify
    assert "FIRE ALARM" in alarm.message


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize(
    "weather",
    ["typical", "harsh"],
)
def test_long_normal_operation_never_alarms(seed, weather):
    """Two weeks, all four nodes: day/night cycles, rain fronts, slow sensor
    drift, noise and one-off gas glitches. Not one WATCH, let alone an ALARM."""
    # harsh: rain/fog every day, 3x the glitches, double the noise and drift.
    sim = HARSH if weather == "harsh" else SIM
    for node in SIM.nodes:
        events = simulate(node, f"{seed}:{node.dev_eui}", 14 * 24 * 60, sim=sim)
        raised = [e.message for e in events if e.to_state in (WATCH, ALARM)]
        assert raised == [], f"{node.name}: {raised}"


# --- the same two, at the deployed cadence ----------------------------------
# The gateway only sees what the (placeholder) node firmware transmits: a
# heartbeat every 10 min, or every minute in alarm mode, duty cycle permitting.

HARSH = SIM.model_copy(update={
    "weather": SIM.weather.model_copy(update={"rain_events_per_day": 1.0}),
    "noise": SIM.noise.model_copy(update={
        "temperature_c": 0.1, "humidity_pct": 0.6, "gas_fraction": 0.04,
        "glitch_probability": 0.006}),
    "drift": SIM.drift.model_copy(update={
        "temperature_c": 0.2, "humidity_pct": 0.8, "gas_fraction": 0.02}),
})


def simulate_uplinks(node, seed, minutes, inject_at=None, sim=SIM):
    fw = NodeFirmware(NodeModel(node, sim, random.Random(seed), T0), sim.firmware,
                      CFG.timing.heartbeat_minutes)
    d = NodeDetector(node.dev_eui, node.name, DET)
    if inject_at is not None:
        fw.model.inject_smoke(inject_at)
    events, t, end = [], T0, T0 + timedelta(minutes=minutes)
    while t < end:
        if (tx := fw.step(t)) is not None:
            events.extend(d.process(t, tx.sample.reading).events)
        t += fw.interval
    return events


@pytest.mark.parametrize("hour", [0, 4, 8, 12, 16, 20])
@pytest.mark.parametrize("node", SIM.nodes, ids=lambda n: n.name)
def test_smoke_alarms_at_heartbeat_cadence(hour, node):
    inject = T0 + timedelta(days=2, hours=hour)
    minutes = int((inject - T0) / STEP) + 30
    events = [e for e in simulate_uplinks(node, f"0:{node.dev_eui}", minutes, inject)
              if e.time >= inject]
    assert [e.to_state for e in events][:2] == [WATCH, ALARM], [e.message for e in events]
    assert events[1].time - inject <= timedelta(minutes=15)


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("weather", ["typical", "harsh"])
def test_long_normal_operation_never_alarms_at_heartbeat_cadence(seed, weather):
    """Two weeks per node at the deployed cadence, alarm-mode bursts included."""
    sim = HARSH if weather == "harsh" else SIM
    for node in SIM.nodes:
        events = simulate_uplinks(node, f"{seed}:{node.dev_eui}", 14 * 24 * 60, sim=sim)
        raised = [e.message for e in events if e.to_state in (WATCH, ALARM)]
        assert raised == [], f"{node.name}: {raised}"


# --- warm-up ---------------------------------------------------------------

def test_no_evaluation_during_warmup():
    d, clock = det(), Clock()
    evals = feed(d, clock, DET.warmup_samples - 1)
    evals += feed(d, clock, 1, **SMOKE)  # last warm-up sample looks like smoke
    assert all(not e.evaluated for e in evals)
    assert d.state == NORMAL  # warm-up done, but nothing was raised
    assert transitions(evals) == [(WARMUP, NORMAL)]
    assert not any(e.notify for ev in evals for e in ev.events)


def test_first_evaluated_reading_is_after_n_samples():
    d, clock = det(), Clock()
    evals = feed(d, clock, DET.warmup_samples + 1)
    assert [e.evaluated for e in evals].index(True) == DET.warmup_samples


# --- channel agreement and sign -------------------------------------------

def test_gas_rise_is_not_smoke():
    # Resistance going UP means cleaner air, even if it is hot and dry.
    d, clock = det(), Clock()
    warm(d, clock)
    evals = feed(d, clock, 30, temp=26.0, hum=35.0, gas=500_000.0)
    assert d.state == NORMAL and transitions(evals) == []


def test_gas_drop_alone_is_not_enough():
    # Rain/fog: gas falls, but it is cooler and more humid.
    d, clock = det(), Clock()
    warm(d, clock)
    feed(d, clock, 30, temp=17.0, hum=75.0, gas=80_000.0)
    assert d.state == NORMAL


def test_heat_and_dryness_without_gas_is_not_enough():
    d, clock = det(), Clock()
    warm(d, clock)
    feed(d, clock, 30, temp=30.0, hum=25.0)
    assert d.state == NORMAL


@pytest.mark.parametrize("secondary", [dict(temp=26.0), dict(hum=35.0)])
def test_gas_drop_plus_either_secondary_channel_alarms(secondary):
    d, clock = det(), Clock()
    warm(d, clock)
    feed(d, clock, 10, gas=80_000.0, **secondary)
    assert d.state == ALARM


def test_smoke_read_against_a_rain_cooled_baseline_still_alarms():
    """Smoke at the start of rain: the lagging baseline makes +6 C read as ~+4.5 C."""
    d, clock = det(), Clock()
    warm(d, clock)
    feed(d, clock, 10, temp=24.5, hum=48.0, gas=80_000.0)
    assert d.state == ALARM


def test_only_relative_change_matters():
    """Same relative event, wildly different absolute levels: same result."""
    def run(t0, h0, g0):
        d, clock = det(), Clock()
        warm(d, clock, temp=t0, hum=h0, gas=g0)
        evals = feed(d, clock, 10, temp=t0 + 6, hum=h0 - 15, gas=g0 * 0.4)
        return [ev.state for ev in evals]

    reference = run(20.0, 50.0, 200_000.0)
    assert reference[-1] == ALARM
    assert run(5.0, 85.0, 20_000.0) == reference
    assert run(38.0, 30.0, 2_000_000.0) == reference


# --- hysteresis ------------------------------------------------------------

def test_watch_then_alarm_with_exact_confirmation_counts():
    d, clock = det(), Clock()
    warm(d, clock)
    evals = feed(d, clock, CONFIRM.watch + CONFIRM.alarm, **SMOKE)
    states = [e.state for e in evals]
    assert states[CONFIRM.watch - 2] == NORMAL
    assert states[CONFIRM.watch - 1] == WATCH  # alarm-level readings still go via WATCH
    assert states[-2] == WATCH
    assert states[-1] == ALARM
    assert transitions(evals) == [(NORMAL, WATCH), (WATCH, ALARM)]


def test_short_burst_below_confirmation_does_nothing():
    d, clock = det(), Clock()
    warm(d, clock)
    for _ in range(5):
        feed(d, clock, CONFIRM.watch - 1, **SMOKE)
        feed(d, clock, 1)  # a normal reading breaks the run
    assert d.state == NORMAL


def test_clearing_steps_down_one_level_at_a_time():
    d, clock = det(), Clock()
    warm(d, clock)
    feed(d, clock, 10, **SMOKE)
    assert d.state == ALARM
    evals = feed(d, clock, 2 * CONFIRM.clear)
    assert transitions(evals) == [(ALARM, WATCH), (WATCH, NORMAL)]
    all_clear = evals[-1].events[0]
    assert all_clear.notify and "back to NORMAL" in all_clear.message


def test_baseline_is_frozen_during_alarm():
    """A 4-hour smoke event must stay in ALARM: the baseline must not chase it."""
    d, clock = det(), Clock()
    warm(d, clock)
    feed(d, clock, 10, **SMOKE)
    frozen = d.baseline
    evals = feed(d, clock, 4 * 60, **SMOKE)
    assert all(e.state == ALARM for e in evals)
    assert d.baseline == frozen


def test_freeze_safety_valve_releases_a_stuck_node():
    """A permanent step change (e.g. sensor replaced) must not hold ALARM forever."""
    d, clock = det(), Clock()
    warm(d, clock)
    evals = feed(d, clock, int(DET.max_baseline_freeze_minutes) + 12 * 60, **SMOKE)
    assert evals[100].state == ALARM
    assert evals[-1].state == NORMAL


def test_slow_drift_over_weeks_is_absorbed():
    """Gas halves and temperature rises 5 C over 3 weeks: sensor ageing, not fire."""
    d, clock = det(), Clock()
    warm(d, clock)
    minutes = 21 * 24 * 60
    for i in range(minutes):
        f = i / minutes
        d.process(clock(), reading(temp=20 + 5 * f, hum=50 - 10 * f, gas=200_000 * (1 - 0.5 * f)))
    assert d.state == NORMAL
    assert d.baseline.gas_ohm == pytest.approx(100_000, rel=0.02)


# --- cooldown --------------------------------------------------------------

def alarm_notifications(evals):
    return [e for ev in evals for e in ev.events if e.to_state == ALARM and e.notify]


def smoke_episode(d, clock):
    evals = feed(d, clock, 10, **SMOKE)
    evals += feed(d, clock, 2 * CONFIRM.clear + 1)
    assert d.state == NORMAL
    return evals


def test_cooldown_suppresses_repeat_alarm_messages():
    d, clock = det(), Clock()
    warm(d, clock)
    first = smoke_episode(d, clock)
    second = smoke_episode(d, clock)  # well inside the cooldown
    assert len(alarm_notifications(first)) == 1
    assert len(alarm_notifications(second)) == 0
    # ...but the state change itself is still reported as an event
    assert any(e.to_state == ALARM for ev in second for e in ev.events)

    feed(d, clock, int(DET.notify_cooldown_minutes))  # wait out the cooldown
    third = smoke_episode(d, clock)
    assert len(alarm_notifications(third)) == 1


# --- time handling ---------------------------------------------------------

def test_ewma_alpha_half_life():
    assert ewma_alpha(120, 120) == pytest.approx(0.5)
    assert ewma_alpha(0, 120) == 0
    assert ewma_alpha(1e6, 120) == pytest.approx(1.0)


def test_baseline_moves_halfway_after_one_half_life():
    d, clock = det(), Clock()
    warm(d, clock)  # baselines at 20 C / 200 kOhm
    hl = DET.baseline_half_life_minutes
    d.process(d.last_time + timedelta(minutes=hl.temperature), reading(temp=21.0))
    assert d.baseline.temperature_c == pytest.approx(20.5)
    d2, clock2 = det(), Clock()
    warm(d2, clock2)
    d2.process(d2.last_time + timedelta(minutes=hl.gas), reading(gas=100_000.0))
    assert d2.baseline.gas_ohm == pytest.approx((200_000 * 100_000) ** 0.5)  # halfway in log space


def test_out_of_order_reading_is_skipped():
    d, clock = det(), Clock()
    warm(d, clock)
    before = d.baseline
    ev = d.process(T0, reading(**SMOKE))  # older than the last reading
    assert not ev.evaluated and d.baseline == before


# --- offline / online ------------------------------------------------------

def test_offline_then_online():
    d, clock = det(), Clock()
    warm(d, clock)
    last = d.last_time
    limit = timedelta(minutes=DET.offline_after_minutes)
    assert d.check_offline(last + limit) is None
    off = d.check_offline(last + limit + STEP)
    assert off.kind == KIND_OFFLINE and off.notify
    assert d.check_offline(last + 2 * limit) is None  # reported once

    ev = d.process(last + 2 * limit, reading())
    assert [e.kind for e in ev.events] == [KIND_ONLINE]


def test_engine_tracks_nodes_independently():
    engine, clock = DetectionEngine(DET), Clock()
    smoke_from = T0 + (DET.warmup_samples + 5) * STEP
    for _ in range(DET.warmup_samples + 20):
        t = clock()
        engine.process("a" * 16, "node-a", t, reading())
        engine.process("b" * 16, "node-b", t, reading(**SMOKE) if t > smoke_from else reading())
    assert engine.nodes["a" * 16].state == NORMAL
    assert engine.nodes["b" * 16].state == ALARM
    offline = engine.check_offline(t + timedelta(hours=1))
    assert {e.node_name for e in offline} == {"node-a", "node-b"}
