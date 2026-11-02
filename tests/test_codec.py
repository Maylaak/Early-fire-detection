import random

import pytest

from gateway.codec import (
    PAYLOAD_LENGTH,
    UINT24_MAX,
    CodecError,
    SensorReading,
    decode,
    encode,
)

TYPICAL = SensorReading(
    temperature_c=23.45,
    humidity_pct=45.67,
    pressure_hpa=1013.2,
    gas_resistance_ohm=123456,
    battery_v=3.70,
)


def assert_close(a: SensorReading, b: SensorReading) -> None:
    """Equal to within each field's encoding resolution."""
    assert a.temperature_c == pytest.approx(b.temperature_c, abs=0.005)
    assert a.humidity_pct == pytest.approx(b.humidity_pct, abs=0.005)
    assert a.pressure_hpa == pytest.approx(b.pressure_hpa, abs=0.05)
    assert a.gas_resistance_ohm == pytest.approx(b.gas_resistance_ohm, abs=0.5)
    assert a.battery_v == pytest.approx(b.battery_v, abs=0.01)


def test_known_bytes():
    # Hand-computed: 2345=0x0929, 4567=0x11D7, 10132-5000=5132=0x140C,
    # 123456=0x01E240, 3.70/0.02=185=0xB9.
    expected = bytes.fromhex("01 0929 11D7 140C 01E240 B9")
    assert encode(TYPICAL) == expected
    assert decode(expected) == TYPICAL


def test_payload_is_11_bytes():
    assert len(encode(TYPICAL)) == PAYLOAD_LENGTH == 11


def test_negative_temperature():
    r = SensorReading(-5.5, 80.0, 950.0, 50000, 3.3)
    payload = encode(r)
    assert payload[1:3] == bytes.fromhex("FDDA")  # -550 as int16
    assert decode(payload).temperature_c == -5.5


@pytest.mark.parametrize("seed", range(5))
def test_round_trip_random(seed):
    rng = random.Random(seed)
    for _ in range(500):
        r = SensorReading(
            temperature_c=rng.uniform(-40, 85),
            humidity_pct=rng.uniform(0, 100),
            pressure_hpa=rng.uniform(500, 1100),
            gas_resistance_ohm=rng.uniform(0, UINT24_MAX),
            battery_v=rng.uniform(0, 5.1),
        )
        assert_close(decode(encode(r)), r)


def test_round_trip_is_stable():
    # Decoding then re-encoding gives the exact same bytes.
    payload = encode(TYPICAL)
    assert encode(decode(payload)) == payload


def test_range_edges():
    lo = SensorReading(-327.68, 0.0, 500.0, 0, 0.0)
    hi = SensorReading(327.67, 100.0, 7053.5, UINT24_MAX, 5.10)
    assert_close(decode(encode(lo)), lo)
    assert_close(decode(encode(hi)), hi)


def test_gas_above_uint24_is_clamped_and_flagged():
    r = SensorReading(20.0, 50.0, 1000.0, 25_000_000, 3.7)
    decoded = decode(encode(r))
    assert decoded.gas_resistance_ohm == UINT24_MAX
    assert decoded.gas_saturated is True
    assert decode(encode(TYPICAL)).gas_saturated is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("temperature_c", 400.0),
        ("temperature_c", -400.0),
        ("humidity_pct", -0.5),
        ("humidity_pct", 100.5),
        ("pressure_hpa", 499.9),  # below the offset: would wrap negative
        ("pressure_hpa", 7054.0),
        ("gas_resistance_ohm", -1),
        ("battery_v", 5.2),
        ("battery_v", -0.1),
    ],
)
def test_encode_rejects_out_of_range(field, value):
    fields = {**TYPICAL.__dict__, field: value}
    with pytest.raises(CodecError):
        encode(SensorReading(**fields))


@pytest.mark.parametrize("length", [0, 10, 12])
def test_decode_rejects_wrong_length(length):
    with pytest.raises(CodecError, match="expected 11 bytes"):
        decode(bytes(length))


def test_decode_rejects_unknown_version():
    payload = bytearray(encode(TYPICAL))
    payload[0] = 2
    with pytest.raises(CodecError, match="version 2"):
        decode(bytes(payload))
