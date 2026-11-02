"""Uplink payload codec. PROVISIONAL FORMAT.

This is the only module that knows the byte layout. When the firmware team
fixes the real format, replace encode()/decode() here and update
tests/test_codec.py; nothing else should need to change.

Format version 1, 11 bytes, big-endian:

    byte  0     format version        uint8   = 1
    bytes 1-2   temperature           int16   0.01 degC
    bytes 3-4   relative humidity     uint16  0.01 %
    bytes 5-6   pressure              uint16  (hPa * 10) - 5000
    bytes 7-9   gas resistance        uint24  ohms
    byte  10    battery               uint8   0.02 V

Notes:
- Pressure below 500.0 hPa cannot be represented (raw would be negative).
  Encoding raises instead of wrapping around.
- Gas resistance above 16 777 215 ohm (uint24 max) is clamped to the max on
  encode; decode() reports that as gas_saturated=True.
- BME688 gas resistance FALLS as VOC/smoke concentration RISES.
"""

from __future__ import annotations

from dataclasses import dataclass

FORMAT_VERSION = 1
PAYLOAD_LENGTH = 11

TEMP_SCALE = 100  # 0.01 degC
HUMIDITY_SCALE = 100  # 0.01 %
PRESSURE_SCALE = 10  # 0.1 hPa
PRESSURE_OFFSET = 5000  # raw = hPa*10 - 5000
BATTERY_STEP_V = 0.02

INT16_MIN, INT16_MAX = -0x8000, 0x7FFF
UINT16_MAX = 0xFFFF
UINT24_MAX = 0xFFFFFF
UINT8_MAX = 0xFF


class CodecError(ValueError):
    """Payload cannot be encoded or decoded."""


@dataclass(frozen=True)
class SensorReading:
    temperature_c: float
    humidity_pct: float
    pressure_hpa: float
    gas_resistance_ohm: float
    battery_v: float
    # True when the gas value hit the uint24 ceiling (real value may be higher).
    gas_saturated: bool = False


def _check(name: str, raw: int, lo: int, hi: int, value: float) -> int:
    if not lo <= raw <= hi:
        raise CodecError(f"{name}={value!r} is outside the encodable range")
    return raw


def encode(reading: SensorReading) -> bytes:
    """Encode a reading into the 11-byte version-1 payload."""
    temp = _check("temperature_c", round(reading.temperature_c * TEMP_SCALE),
                  INT16_MIN, INT16_MAX, reading.temperature_c)
    # The sensor never reports outside 0-100 %RH; anything else is a bug upstream.
    hum = _check("humidity_pct", round(reading.humidity_pct * HUMIDITY_SCALE),
                 0, 100 * HUMIDITY_SCALE, reading.humidity_pct)
    pres = _check("pressure_hpa", round(reading.pressure_hpa * PRESSURE_SCALE) - PRESSURE_OFFSET,
                  0, UINT16_MAX, reading.pressure_hpa)
    if reading.gas_resistance_ohm < 0:
        raise CodecError(f"gas_resistance_ohm={reading.gas_resistance_ohm!r} is negative")
    gas = min(round(reading.gas_resistance_ohm), UINT24_MAX)
    batt = _check("battery_v", round(reading.battery_v / BATTERY_STEP_V),
                  0, UINT8_MAX, reading.battery_v)

    return (
        FORMAT_VERSION.to_bytes(1, "big")
        + temp.to_bytes(2, "big", signed=True)
        + hum.to_bytes(2, "big")
        + pres.to_bytes(2, "big")
        + gas.to_bytes(3, "big")
        + batt.to_bytes(1, "big")
    )


def decode(payload: bytes) -> SensorReading:
    """Decode an 11-byte version-1 payload. Raises CodecError on bad input."""
    if len(payload) != PAYLOAD_LENGTH:
        raise CodecError(f"expected {PAYLOAD_LENGTH} bytes, got {len(payload)}")
    version = payload[0]
    if version != FORMAT_VERSION:
        raise CodecError(f"unsupported payload format version {version}")

    temp = int.from_bytes(payload[1:3], "big", signed=True)
    hum = int.from_bytes(payload[3:5], "big")
    pres = int.from_bytes(payload[5:7], "big")
    gas = int.from_bytes(payload[7:10], "big")
    batt = payload[10]

    return SensorReading(
        temperature_c=temp / TEMP_SCALE,
        humidity_pct=hum / HUMIDITY_SCALE,
        pressure_hpa=(pres + PRESSURE_OFFSET) / PRESSURE_SCALE,
        gas_resistance_ohm=float(gas),
        battery_v=round(batt * BATTERY_STEP_V, 2),
        gas_saturated=gas == UINT24_MAX,
    )
