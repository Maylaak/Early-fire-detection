"""ChirpStack v4 MQTT integration format: uplink topic and JSON event envelope.

The simulator builds messages here and ingestion parses them here, so swapping
the simulator for a real ChirpStack v4 server needs only a config change
(MQTT host/port). ChirpStack must have JSON output enabled:

    # chirpstack.toml
    [integration.mqtt]
    json = true

Shape of a v4 "up" event (fields we don't use are kept for realism):

    {
      "deduplicationId": "...uuid...",
      "time": "2026-11-01T12:00:00.123456+00:00",
      "deviceInfo": {"tenantId", "tenantName", "applicationId" (UUID),
                     "applicationName", "deviceProfileId", "deviceProfileName",
                     "deviceName", "devEui" (lowercase hex), "tags"},
      "devAddr": "260b0001", "adr": true, "dr": 5,
      "fCnt": 12, "fPort": 2, "confirmed": false,
      "data": "<base64 of the raw payload>",
      "rxInfo": [{"gatewayId", "uplinkId", "rssi", "snr", "channel", ...}],
      "txInfo": {"frequency", "modulation": {"lora": {...}}}
    }
"""

from __future__ import annotations

import base64
import binascii
import math
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Placeholder tenant / device-profile IDs for simulated messages.
SIM_TENANT_ID = "52f14cd4-c6f1-4fbd-8f87-4025e1d49242"
SIM_DEVICE_PROFILE_ID = "6a2c8f0e-1d7b-4b3e-9e65-0f6f1c2a7b11"

# EU868 default channels.
EU868_CHANNELS_HZ = (868_100_000, 868_300_000, 868_500_000)

UPLINK_TOPIC_RE = re.compile(r"^application/([^/]+)/device/([0-9a-fA-F]{16})/event/up$")


class EnvelopeError(ValueError):
    """MQTT message is not a usable ChirpStack v4 uplink."""


@dataclass(frozen=True)
class RxGateway:
    """One gateway that heard the uplink (an rxInfo entry)."""

    gateway_id: str
    rssi: int | None
    snr: float | None


@dataclass(frozen=True)
class Uplink:
    dev_eui: str
    device_name: str
    application_id: str
    application_name: str
    dev_addr: str | None
    f_cnt: int
    f_port: int
    data: bytes
    rssi: int | None  # best gateway
    snr: float | None  # best gateway
    gateway_count: int
    time: datetime
    deduplication_id: str | None
    gateways: tuple[RxGateway, ...] = ()


def parse_uplink(topic: str, body: bytes, received_at: datetime) -> Uplink:
    """Parse a ChirpStack v4 JSON uplink event.

    Defensive by design: ChirpStack's JSON may omit fields that hold their
    default value (e.g. fCnt 0), so missing integers are read as 0, and a
    missing or unparseable "time" falls back to `received_at`.
    """
    m = UPLINK_TOPIC_RE.match(topic)
    if not m:
        raise EnvelopeError(f"not an uplink topic: {topic!r}")
    topic_app_id, topic_dev_eui = m.group(1), m.group(2).lower()

    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise EnvelopeError(f"invalid JSON: {e}") from e
    if not isinstance(msg, dict):
        raise EnvelopeError("JSON body is not an object")

    info = msg.get("deviceInfo") if isinstance(msg.get("deviceInfo"), dict) else {}
    dev_eui = str(info.get("devEui") or topic_dev_eui).lower()
    if dev_eui != topic_dev_eui:
        raise EnvelopeError(f"devEui {dev_eui} does not match topic ({topic_dev_eui})")

    data_b64 = msg.get("data")
    if not data_b64:
        raise EnvelopeError("no application payload (data) in uplink")
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError, TypeError) as e:
        raise EnvelopeError(f"data is not valid base64: {e}") from e

    rx = [r for r in msg.get("rxInfo") or [] if isinstance(r, dict) and r.get("rssi") is not None]
    best = max(rx, key=lambda r: r["rssi"]) if rx else None

    try:
        f_cnt = int(msg.get("fCnt") or 0)
        f_port = int(msg.get("fPort") or 0)
        rssi = int(best["rssi"]) if best else None
        snr = float(best["snr"]) if best and best.get("snr") is not None else None
        gateways = tuple(
            RxGateway(str(r["gatewayId"]).lower(), int(r["rssi"]),
                      float(r["snr"]) if r.get("snr") is not None else None)
            for r in rx if r.get("gatewayId")
        )
    except (TypeError, ValueError) as e:
        raise EnvelopeError(f"bad numeric field: {e}") from e

    return Uplink(
        dev_eui=dev_eui,
        device_name=str(info.get("deviceName") or dev_eui),
        application_id=str(info.get("applicationId") or topic_app_id),
        application_name=str(info.get("applicationName") or ""),
        dev_addr=msg.get("devAddr"),
        f_cnt=f_cnt,
        f_port=f_port,
        data=data,
        rssi=rssi,
        snr=snr,
        gateway_count=len(rx),
        time=_parse_time(msg.get("time"), received_at),
        deduplication_id=msg.get("deduplicationId"),
        gateways=gateways,
    )


def _parse_time(value: Any, fallback: datetime) -> datetime:
    if not isinstance(value, str):
        return fallback
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return fallback
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def uplink_topic(application_id: str, dev_eui: str) -> str:
    return f"application/{application_id}/device/{dev_eui.lower()}/event/up"


def eu868_data_rate(spreading_factor: int) -> int:
    """EU868 at 125 kHz: DR0 = SF12 ... DR5 = SF7."""
    if not 7 <= spreading_factor <= 12:
        raise ValueError(f"spreading factor {spreading_factor} not in 7..12")
    return 12 - spreading_factor


LORAWAN_OVERHEAD_BYTES = 13  # MHDR 1 + FHDR 7 (no FOpts) + FPort 1 + MIC 4


def lora_airtime_seconds(spreading_factor: int, app_payload_bytes: int) -> float:
    """Time on air of one uplink (Semtech AN1200.13): EU868 125 kHz, CR 4/5,
    8-symbol preamble, explicit header, CRC on, low-data-rate optimisation at SF11/12."""
    if not 7 <= spreading_factor <= 12:
        raise ValueError(f"spreading factor {spreading_factor} not in 7..12")
    sf, pl = spreading_factor, app_payload_bytes + LORAWAN_OVERHEAD_BYTES
    t_sym = 2**sf / 125_000
    de = 1 if sf >= 11 else 0
    n_payload = 8 + max(math.ceil((8 * pl - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * 5, 0)
    return (8 + 4.25 + n_payload) * t_sym


def duty_cycle_gap_seconds(spreading_factor: int, app_payload_bytes: int, duty_cycle: float) -> float:
    """Shortest legal time from the start of one uplink to the start of the next."""
    return lora_airtime_seconds(spreading_factor, app_payload_bytes) / duty_cycle


def build_uplink(
    *,
    application_id: str,
    application_name: str,
    device_name: str,
    dev_eui: str,
    dev_addr: str,
    f_cnt: int,
    f_port: int,
    payload: bytes,
    rssi: int,
    snr: float,
    time: datetime,
    gateway_id: str,
    spreading_factor: int = 7,
) -> dict[str, Any]:
    """Build a ChirpStack v4 uplink event as a JSON-ready dict."""
    return {
        "deduplicationId": str(uuid.uuid4()),
        "time": time.isoformat(),
        "deviceInfo": {
            "tenantId": SIM_TENANT_ID,
            "tenantName": "LAU VIP",
            "applicationId": application_id,
            "applicationName": application_name,
            "deviceProfileId": SIM_DEVICE_PROFILE_ID,
            "deviceProfileName": "bme688-node",
            "deviceName": device_name,
            "devEui": dev_eui.lower(),
            "tags": {},
        },
        "devAddr": dev_addr.lower(),
        "adr": True,
        "dr": eu868_data_rate(spreading_factor),
        "fCnt": f_cnt,
        "fPort": f_port,
        "confirmed": False,
        "data": base64.b64encode(payload).decode("ascii"),
        "rxInfo": [
            {
                "gatewayId": gateway_id,
                "uplinkId": uuid.uuid4().int & 0xFFFFFFFF,
                "nsTime": time.isoformat(),
                "rssi": rssi,
                "snr": snr,
                "channel": f_cnt % len(EU868_CHANNELS_HZ),
                "context": "AAAAAA==",
                "metadata": {},
                "crcStatus": "CRC_OK",
            }
        ],
        "txInfo": {
            "frequency": EU868_CHANNELS_HZ[f_cnt % len(EU868_CHANNELS_HZ)],
            "modulation": {
                "lora": {
                    "bandwidth": 125000,
                    "spreadingFactor": spreading_factor,
                    "codeRate": "CR_4_5",
                }
            },
        },
    }
