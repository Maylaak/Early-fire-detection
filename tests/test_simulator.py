import base64
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from gateway.chirpstack import build_uplink, duty_cycle_gap_seconds, lora_airtime_seconds, uplink_topic
from gateway.codec import decode, encode
from gateway.config import DEFAULT_CONFIG_PATH, load_config
from gateway.sim.firmware import ALARM_MODE, HEARTBEAT, NodeFirmware
from gateway.sim.model import NodeModel, adr_spreading_factor

CFG = load_config(DEFAULT_CONFIG_PATH, env={})
SIM = CFG.simulator
NODE = SIM.nodes[0]
T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
STEP = timedelta(seconds=60)


def make_model(seed=1, node=NODE):
    return NodeModel(node, SIM, random.Random(seed), T0)


def run(model, n, start=T0):
    return [model.sample(start + i * STEP) for i in range(n)]


def test_topic_matches_chirpstack_v4():
    assert (
        uplink_topic(SIM.application_id, "70B3D57ED0000001")
        == f"application/{SIM.application_id}/device/70b3d57ed0000001/event/up"
    )


def test_envelope_shape_and_payload():
    s = make_model().sample(T0)
    env = build_uplink(
        application_id=SIM.application_id, application_name=SIM.application_name,
        device_name=NODE.name, dev_eui=NODE.dev_eui, dev_addr=NODE.dev_addr,
        f_cnt=s.f_cnt, f_port=CFG.lorawan.heartbeat_f_port, payload=encode(s.reading),
        rssi=s.rssi, snr=s.snr, time=s.time, gateway_id=SIM.gateway_id,
        spreading_factor=10,
    )
    info = env["deviceInfo"]
    assert info["applicationName"] == SIM.application_name
    assert info["deviceName"] == NODE.name
    assert info["devEui"] == NODE.dev_eui
    assert env["devAddr"] == NODE.dev_addr
    assert env["fCnt"] == 0 and env["fPort"] == CFG.lorawan.heartbeat_f_port
    assert env["dr"] == 2  # EU868 SF10
    assert isinstance(env["rxInfo"], list)
    assert env["rxInfo"][0]["rssi"] == s.rssi and env["rxInfo"][0]["snr"] == s.snr
    assert datetime.fromisoformat(env["time"]) == T0
    assert decode(base64.b64decode(env["data"])) == decode(encode(s.reading))


def test_alarm_mode_uplinks_use_their_own_port():
    from gateway.sim.__main__ import _envelope
    model = make_model()
    s = model.sample(T0)
    assert _envelope(CFG, model, s, HEARTBEAT)["fPort"] == CFG.lorawan.heartbeat_f_port
    assert _envelope(CFG, model, s, ALARM_MODE)["fPort"] == CFG.lorawan.alarm_mode_f_port


def test_f_cnt_increments():
    assert [s.f_cnt for s in run(make_model(), 5)] == [0, 1, 2, 3, 4]


def test_same_seed_is_reproducible():
    a = [s.reading for s in run(make_model(seed=7), 200)]
    b = [s.reading for s in run(make_model(seed=7), 200)]
    assert a == b


def test_smoke_drops_gas_raises_temperature_lowers_humidity():
    # Same seed => identical noise; the only difference is the smoke.
    clean, smoky = make_model(seed=3), make_model(seed=3)
    smoky.inject_smoke(T0 + 30 * STEP)
    full = 30 + int(SIM.smoke.ramp_minutes) + 2  # well inside the hold phase
    c, s = run(clean, full + 1)[full], run(smoky, full + 1)[full]

    assert s.smoke_level == 1.0
    # Smoke makes BME688 gas resistance FALL.
    assert s.reading.gas_resistance_ohm < 0.6 * c.reading.gas_resistance_ohm
    assert s.reading.temperature_c > c.reading.temperature_c + 0.9 * SIM.smoke.temperature_rise_c
    assert s.reading.humidity_pct < c.reading.humidity_pct - 0.9 * SIM.smoke.humidity_drop_pct


def test_smoke_event_ends():
    m = make_model()
    m.inject_smoke(T0, hold_minutes=1)
    samples = run(m, 30)
    assert samples[3].smoke_level > 0
    assert samples[-1].smoke_level == 0
    assert not m.smoke_active(samples[-1].time)


def test_rain_events_occur():
    # Rain/fog is what the no-false-alarm test leans on (gas falls with humidity).
    samples = run(make_model(), 14 * 24 * 60)
    assert any(s.raining for s in samples), "expected at least one rain event in two weeks"


@pytest.mark.parametrize("node", SIM.nodes, ids=lambda n: n.name)
def test_two_weeks_of_readings_stay_encodable(node):
    for s in run(make_model(node=node), 14 * 24 * 60):
        r = s.reading
        assert 0 <= r.humidity_pct <= 100
        assert r.gas_resistance_ohm > 0
        assert decode(encode(r)).temperature_c == pytest.approx(r.temperature_c, abs=0.005)


def test_day_is_warmer_and_drier_than_night():
    m = make_model()
    samples = run(m, 24 * 60)
    by_hour = {s.time.hour: s.reading for s in samples}
    peak = int(SIM.diurnal.peak_hour_utc)
    night = (peak + 12) % 24
    assert by_hour[peak].temperature_c > by_hour[night].temperature_c + 5
    assert by_hour[peak].humidity_pct < by_hour[night].humidity_pct - 10


@pytest.mark.parametrize("snr,sf", [(7.5, 7), (2.0, 8), (-6.5, 11), (-25.0, 12)])
def test_adr_spreading_factor(snr, sf):
    assert adr_spreading_factor(snr) == sf


# --- airtime and duty cycle -------------------------------------------------------

@pytest.mark.parametrize("sf,ms", [(7, 61.7), (9, 205.8), (11, 823.3), (12, 1482.8)])
def test_airtime_matches_published_calculator(sf, ms):
    # 11-byte payload + 13 bytes LoRaWAN overhead, EU868 125 kHz, CR 4/5.
    assert lora_airtime_seconds(sf, 11) * 1000 == pytest.approx(ms, abs=0.1)


def test_one_percent_duty_cycle_gap():
    assert duty_cycle_gap_seconds(12, 11, 0.01) == pytest.approx(148.3, abs=0.1)


# --- placeholder firmware: heartbeat + event-driven -------------------------------

FW = SIM.firmware
HB = CFG.timing.heartbeat_minutes


def firmware(node=NODE, seed=1, speedup=1.0, fw=FW, hb=HB):
    return NodeFirmware(make_model(seed, node), fw, hb, speedup)


def drive(fw, minutes, start=T0):
    out, t, end = [], start, start + timedelta(minutes=minutes)
    while t < end:
        if (tx := fw.step(t)) is not None:
            out.append(tx)
        t += fw.interval
    return out


def gaps_minutes(txs):
    return [(b.sample.time - a.sample.time).total_seconds() / 60 for a, b in zip(txs, txs[1:])]


def test_quiet_node_sends_only_heartbeats():
    txs = drive(firmware(), 24 * 60)
    assert {t.reason for t in txs} == {HEARTBEAT}
    assert set(gaps_minutes(txs)) == {HB}
    assert [t.sample.f_cnt for t in txs] == list(range(len(txs)))  # uplink frame counter


def test_smoke_switches_to_alarm_mode_within_minutes():
    fw = firmware()
    inject = T0 + timedelta(hours=3)
    fw.model.inject_smoke(inject)
    txs = drive(fw, 4 * 60)
    first = next(t for t in txs if t.reason == ALARM_MODE)
    # Sent at once, not at the next heartbeat.
    assert first.sample.time - inject <= timedelta(minutes=3)
    during = [t for t in txs if inject <= t.sample.time <= inject + timedelta(minutes=30)]
    assert len(during) >= 25  # ~1 per minute instead of 3 heartbeats
    # Back to heartbeats after the smoke and the hold period.
    assert txs[-1].reason == HEARTBEAT


def test_single_gas_glitch_does_not_trigger_alarm_mode():
    # Reproduce a one-off glitch: one read at half the resistance.
    fw = firmware()
    drive(fw, 60)
    model = fw.model
    real = model.sample

    def glitched(t):
        s = real(t)
        return replace(s, reading=replace(s.reading, gas_resistance_ohm=s.reading.gas_resistance_ohm * 0.5))

    model.sample = glitched
    fw.step(T0 + timedelta(minutes=60))
    model.sample = real
    drive(fw, 30, T0 + timedelta(minutes=60, seconds=FW.sample_seconds))
    assert fw.triggers == 0


def test_duty_cycle_limits_slow_nodes_in_alarm_mode():
    sf12 = NODE.model_copy(update={"snr_db": -25.0})
    fw = firmware(node=sf12)
    assert fw.model.spreading_factor == 12
    inject = T0 + timedelta(hours=3)
    fw.model.inject_smoke(inject)
    txs = [t for t in drive(fw, 4 * 60) if t.sample.time >= inject]
    gaps = gaps_minutes(txs)
    assert min(gaps) * 60 >= duty_cycle_gap_seconds(12, 11, FW.duty_cycle)
    assert min(gaps) > FW.alarm_interval_minutes  # cannot keep a 1-minute rate at SF12


def test_demo_speedup_scales_firmware_timing():
    fast = load_config(DEFAULT_CONFIG_PATH, env={"DEMO_SPEEDUP": "60"})
    fw = NodeFirmware(NodeModel(NODE, fast.simulator, random.Random(1), T0),
                      fast.simulator.firmware, fast.timing.heartbeat_minutes, 60)
    assert fw.interval == timedelta(seconds=FW.sample_seconds / 60)
    txs = drive(fw, 5)
    assert gaps_minutes(txs) == pytest.approx([HB / 60] * (len(txs) - 1))