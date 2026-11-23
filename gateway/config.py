"""Load and validate config.yaml.

This module only describes the *shape* of the configuration. Every value
lives in the YAML file. Unknown keys are rejected so a typo in a threshold
name fails at startup instead of silently falling back to nothing.

Two values are derived here rather than written in the YAML:
- detection.offline_after_minutes = timing.heartbeat_minutes x
  timing.missed_heartbeats_for_missing (one source of truth for "missing").
- With timing.demo_speedup > 1, every wall-clock duration is divided by it
  (see apply_speedup). Sample counts such as warm-up are NOT scaled.

    python -m gateway.config      prints the effective settings
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
CONFIG_PATH_ENV = "WILDFIRE_CONFIG"

# Environment variable -> (section, key). Applied before validation.
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "DATABASE_URL": ("database", "url"),
    "WEB_DATABASE_URL": ("database", "web_url"),
    "MQTT_HOST": ("mqtt", "host"),
    "MQTT_PORT": ("mqtt", "port"),
    "DEMO_SPEEDUP": ("timing", "demo_speedup"),
}

State = Literal["NORMAL", "WATCH", "ALARM"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _hex(value: str, length: int, what: str) -> str:
    if not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value):
        raise ValueError(f"{what} must be {length} hex characters")
    return value.lower()


def _unique(values: list[str], what: str) -> None:
    dupes = sorted({v for v in values if values.count(v) > 1})
    if dupes:
        raise ValueError(f"duplicate {what}: {', '.join(dupes)}")


class TimingConfig(_Strict):
    # How often a node reports when nothing is happening (LoRaWAN duty cycle
    # and battery). Nodes also transmit early, on their own local alarm.
    heartbeat_minutes: float = Field(gt=0)
    # A node is MISSING after this many heartbeats with no uplink.
    missed_heartbeats_for_missing: int = Field(ge=1)
    # 1 = real time. N = every wall-clock duration divided by N, for demos.
    demo_speedup: float = Field(ge=1, le=3600)
    # How often the detection service writes a "system check" line to the console log.
    system_check_minutes: float = Field(gt=0)

    @property
    def missing_after_minutes(self) -> float:
        return self.heartbeat_minutes * self.missed_heartbeats_for_missing


class MqttConfig(_Strict):
    host: str
    port: int = Field(gt=0, lt=65536)
    uplink_topic: str
    qos: int = Field(ge=0, le=2)
    keepalive_seconds: int = Field(gt=0)
    # Fixed MQTT client id of the detection service. With it the broker keeps
    # the service's session (and queues uplinks) while the service is down.
    # One detection service per broker: two with the same id knock each other off.
    service_client_id: str = Field(min_length=1, max_length=23)


class DatabaseConfig(_Strict):
    url: str
    web_url: str
    # Database writes the detection service can hold while the database is
    # down. Past this, the oldest reading writes are dropped (never events).
    max_queued_writes: int = Field(ge=100)


class SimNoise(_Strict):
    temperature_c: float = Field(ge=0)
    humidity_pct: float = Field(ge=0)
    pressure_hpa: float = Field(ge=0)
    gas_fraction: float = Field(ge=0, lt=1)
    rssi_db: float = Field(ge=0)
    snr_db: float = Field(ge=0)
    glitch_probability: float = Field(ge=0, le=1)


class SimDrift(_Strict):
    temperature_c: float = Field(ge=0)
    humidity_pct: float = Field(ge=0)
    pressure_hpa: float = Field(ge=0)
    gas_fraction: float = Field(ge=0, lt=1)


class SimDiurnal(_Strict):
    peak_hour_utc: float = Field(ge=0, lt=24)
    temperature_amplitude_c: float = Field(ge=0)
    humidity_amplitude_pct: float = Field(ge=0)
    gas_humidity_coeff: float = Field(ge=0)


class SimWeather(_Strict):
    rain_events_per_day: float = Field(ge=0)
    rain_ramp_hours: float = Field(gt=0)
    rain_hold_hours: float = Field(ge=0)
    rain_humidity_rise_pct: float = Field(ge=0)
    rain_temperature_drop_c: float = Field(ge=0)


class SimSmoke(_Strict):
    ramp_minutes: float = Field(gt=0)
    hold_minutes: float = Field(ge=0)
    gas_drop_fraction: float = Field(gt=0, lt=1)
    temperature_rise_c: float = Field(ge=0)
    humidity_drop_pct: float = Field(ge=0)


class LocalTrigger(_Strict):
    baseline_half_life_minutes: float = Field(gt=0)
    warmup_samples: int = Field(ge=1)
    gas_drop_pct: float = Field(gt=0, lt=100)
    confirm_samples: int = Field(ge=1)


class SimFirmware(_Strict):
    """PLACEHOLDER node firmware behaviour: decides WHEN a simulated node transmits."""

    sample_seconds: float = Field(gt=0)
    local_trigger: LocalTrigger
    # Alarm mode has two rates: while the local trigger holds, and (slower)
    # after it clears, until alarm_hold_minutes have passed.
    interval_while_triggered_seconds: float = Field(gt=0)
    interval_after_trigger_minutes: float = Field(gt=0)
    alarm_hold_minutes: float = Field(ge=0)
    duty_cycle: float = Field(gt=0, le=1)


class SimNode(_Strict):
    name: str
    dev_eui: str
    dev_addr: str
    temperature_c: float
    humidity_pct: float = Field(ge=0, le=100)
    pressure_hpa: float = Field(gt=0)
    gas_resistance_ohm: float = Field(gt=0)
    battery_v: float = Field(gt=0)
    rssi_dbm: float
    snr_db: float

    @field_validator("dev_eui")
    @classmethod
    def _dev_eui_hex(cls, v: str) -> str:
        # ChirpStack v4 reports devEui as 16 lowercase hex characters.
        return _hex(v, 16, "dev_eui")

    @field_validator("dev_addr")
    @classmethod
    def _dev_addr_hex(cls, v: str) -> str:
        return _hex(v, 8, "dev_addr")


class LorawanConfig(_Strict):
    # The node firmware's contract: which fPort means what (1-223 are
    # application ports).
    heartbeat_f_port: int = Field(ge=1, le=223)
    alarm_mode_f_port: int = Field(ge=1, le=223)

    @model_validator(mode="after")
    def _distinct(self) -> LorawanConfig:
        if self.heartbeat_f_port == self.alarm_mode_f_port:
            raise ValueError("heartbeat_f_port and alarm_mode_f_port must differ: "
                             "the port is how alarm-mode uplinks are recognised")
        return self


class SimulatorConfig(_Strict):
    reporting_mode: Literal["legacy", "event_driven"] = "legacy"
    state_path: str = "data/simulator-state.sqlite"

    application_id: str
    application_name: str
    gateway_id: str
    control_topic: str
    seed: int | None
    noise: SimNoise
    drift: SimDrift
    diurnal: SimDiurnal
    weather: SimWeather
    battery_drain_v_per_day: float = Field(ge=0)
    smoke: SimSmoke
    firmware: SimFirmware
    nodes: list[SimNode] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_nodes(self) -> SimulatorConfig:
        euis = [n.dev_eui for n in self.nodes]
        names = [n.name for n in self.nodes]
        if len(set(euis)) != len(euis):
            raise ValueError("simulator node dev_eui values must be unique")
        if len(set(names)) != len(names):
            raise ValueError("simulator node names must be unique")
        return self


class LevelThresholds(_Strict):
    gas_drop_pct: float = Field(gt=0, lt=100)
    temperature_rise_c: float = Field(gt=0)
    humidity_drop_pct: float = Field(gt=0)


class ConfirmSamples(_Strict):
    watch: int = Field(ge=1)
    alarm: int = Field(ge=1)
    clear: int = Field(ge=1)


class HalfLives(_Strict):
    gas: float = Field(gt=0)
    temperature: float = Field(gt=0)
    humidity: float = Field(gt=0)


class DetectionConfig(_Strict):
    # Explicit control protocol uses real daily heartbeats; legacy demo timing is unchanged.
    explicit_heartbeat_minutes: float = Field(default=1440, gt=0)
    explicit_missed_heartbeats: int = Field(default=3, ge=1)

    @property
    def explicit_missing_minutes(self) -> float:
        return self.explicit_heartbeat_minutes * self.explicit_missed_heartbeats

    baseline_half_life_minutes: HalfLives
    warmup_samples: int = Field(ge=1)
    replay_hours_on_startup: float = Field(ge=0)
    freeze_baseline_states: list[State]
    max_baseline_freeze_minutes: float = Field(gt=0)
    watch: LevelThresholds
    alarm: LevelThresholds
    confirm_samples: ConfirmSamples
    notify_cooldown_minutes: float = Field(ge=0)
    offline_after_minutes: float = Field(gt=0)

    @model_validator(mode="after")
    def _alarm_stricter_than_watch(self) -> DetectionConfig:
        w, a = self.watch, self.alarm
        if not (
            a.gas_drop_pct >= w.gas_drop_pct
            and a.temperature_rise_c >= w.temperature_rise_c
            and a.humidity_drop_pct >= w.humidity_drop_pct
        ):
            raise ValueError("every alarm threshold must be at least its watch threshold")
        return self


class TelegramConfig(_Strict):
    bot_token_env: str
    chat_id_env: str
    timeout_seconds: float = Field(gt=0)


class EmailConfig(_Strict):
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, gt=0, lt=65536)
    # starttls (port 587), ssl (port 465), or none (local relay only).
    security: Literal["starttls", "ssl", "none"] = "starttls"
    # Names of the environment variables holding the login, password and
    # addresses (never the values themselves).
    username_env: str = "ALERT_EMAIL_USER"
    password_env: str = "ALERT_EMAIL_PASSWORD"
    from_env: str = "ALERT_EMAIL_FROM"
    to_env: str = "ALERT_EMAIL_TO"
    timeout_seconds: float = Field(default=15, gt=0)


class NotifiersConfig(_Strict):
    enabled: list[Literal["console", "telegram", "email", "sms"]]
    telegram: TelegramConfig
    email: EmailConfig = EmailConfig()


class WebConfig(_Strict):
    host: str
    port: int = Field(gt=0, lt=65536)
    poll_seconds: float = Field(gt=0)
    chart_hours: float = Field(gt=0)


# --- site registry: what operators see instead of hex identifiers ----------

class _Located(_Strict):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class SiteNode(_Located):
    dev_eui: str
    label: str = Field(min_length=1)  # e.g. "F-07-03"
    block: str = Field(min_length=1)  # e.g. "F-07"; the console calls it a cluster
    # The gateway this node's cluster belongs to. Optional when there is only
    # one gateway: every node is then assigned to it.
    gateway_id: str | None = None
    # True when lat/lon are the node's real GPS position (entered in
    # config.yaml), False when they are a planned grid position.
    surveyed: bool = False

    @field_validator("dev_eui")
    @classmethod
    def _dev_eui_hex(cls, v: str) -> str:
        return _hex(v, 16, "dev_eui")

    @field_validator("gateway_id")
    @classmethod
    def _node_gateway_hex(cls, v: str | None) -> str | None:
        return None if v is None else _hex(v, 16, "gateway_id")


class SiteGateway(_Located):
    gateway_id: str
    label: str = Field(min_length=1)

    @field_validator("gateway_id")
    @classmethod
    def _gateway_id_hex(cls, v: str) -> str:
        return _hex(v, 16, "gateway_id")


class SiteCentre(_Located):
    label: str = Field(min_length=1)
    entity_id: str = "MCU-01"
    role: str = "Main Control Unit"
    status: Literal["planned"] = "planned"
    provisional: Literal[True] = True


class SiteDrone(_Located):
    entity_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    status: Literal["planned"] = "planned"
    provisional: Literal[True] = True


class DemoOffset(_Strict):
    east_m: float = Field(ge=-10000, le=10000)
    north_m: float = Field(ge=-10000, le=10000)


class DemoLayout(_Strict):
    centre: _Located                      # centre of the 3 x 3 pilot grid
    # A single gateway without lat/lon is placed this far from the grid centre.
    # With several gateways, give each one its lat/lon under sites.gateways.
    gateway_distance_m: float | None = Field(default=None, ge=0, le=10000)
    gateway_bearing_deg: float | None = Field(default=None, ge=0, lt=360)
    # Theoretical gateway range (docs/GATEWAY-COVERAGE.md); drawn on the map.
    gateway_range_m: float | None = Field(default=None, gt=0, le=20000)
    # Reserve outline [[lat, lon], ...], split into one sector per gateway.
    reserve: list[tuple[float, float]] | None = None
    # Offset from the grid centre ({east_m, north_m}) or a position ({lat, lon}).
    drone: DemoOffset | _Located
    control_unit: DemoOffset | _Located

    @model_validator(mode="after")
    def _reserve_ring(self) -> DemoLayout:
        if self.reserve is not None:
            if len(self.reserve) < 3:
                raise ValueError("demo_layout.reserve needs at least three points")
            for lat, lon in self.reserve:
                _Located.model_validate({"lat": lat, "lon": lon})
        if (self.gateway_distance_m is None) != (self.gateway_bearing_deg is None):
            raise ValueError("demo_layout: set both gateway_distance_m and gateway_bearing_deg, or neither")
        return self



class SitesConfig(_Strict):
    nodes: list[SiteNode]
    gateways: list[SiteGateway]
    centre: SiteCentre
    drone: SiteDrone | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> SitesConfig:
        _unique([n.dev_eui for n in self.nodes], "site node dev_eui")
        _unique([n.label for n in self.nodes], "site node label")
        _unique([g.gateway_id for g in self.gateways], "gateway_id")
        known = {g.gateway_id for g in self.gateways}
        for n in self.nodes:
            if n.gateway_id is None:
                raise ValueError(f"site node {n.label}: set gateway_id (there is more than one gateway)")
            if n.gateway_id not in known:
                raise ValueError(f"site node {n.label}: gateway_id {n.gateway_id} is not a site gateway")
        return self

    @model_validator(mode="before")
    @classmethod
    def _default_gateway(cls, raw):
        """With a single gateway, every node without gateway_id belongs to it."""
        if not isinstance(raw, dict):
            return raw
        gws, nodes = raw.get("gateways"), raw.get("nodes")
        if not (isinstance(gws, list) and len(gws) == 1 and isinstance(nodes, list)):
            return raw
        only = gws[0].get("gateway_id") if isinstance(gws[0], dict) else getattr(gws[0], "gateway_id", None)
        if only is None:
            return raw
        out = []
        for node in nodes:
            if isinstance(node, dict) and node.get("gateway_id") is None:
                node = {**node, "gateway_id": only}
            elif isinstance(node, SiteNode) and node.gateway_id is None:
                node = node.model_copy(update={"gateway_id": _hex(only, 16, "gateway_id")})
            out.append(node)
        return {**raw, "nodes": out}

    def gateway(self, gateway_id: str | None) -> SiteGateway | None:
        return next((g for g in self.gateways if g.gateway_id == gateway_id), None)

    def node(self, dev_eui: str) -> SiteNode | None:
        dev_eui = dev_eui.lower()
        return next((n for n in self.nodes if n.dev_eui == dev_eui), None)

    def label_for(self, dev_eui: str, fallback: str) -> str:
        site = self.node(dev_eui)
        return site.label if site else fallback


# --- operator console ------------------------------------------------------

class TileLayer(_Strict):
    max_native_zoom: int | None = Field(default=None, ge=1, le=22)
    url: str
    attribution: str
    max_zoom: int = Field(ge=1, le=22)


class MapConfig(_Strict):
    show_nodes_in_overview: bool = False

    centre_lat: float = Field(ge=-90, le=90)
    centre_lon: float = Field(ge=-180, le=180)
    zoom: int = Field(ge=1, le=20)
    # Legacy detail/clustering threshold. Demo mode uses at least zoom 18
    # (capped by tile limits), with individual nodes and an overview summary.
    node_detail_zoom: int = Field(ge=2, le=22)
    # Nearby node markers closer than this (screen pixels) share one badge.
    cluster_radius_px: int = Field(ge=10, le=200)
    # Sensor coverage radius, NOT fire extent or the demo alarm-highlight radius.
    alarm_zone_radius_m: float = Field(gt=0)
    primary_tiles: TileLayer
    fallback_tiles: TileLayer

    @model_validator(mode="after")
    def _detail_zoom_reachable(self) -> MapConfig:
        if self.node_detail_zoom <= self.zoom:
            raise ValueError("node_detail_zoom must be greater than zoom: the default view hides nodes not in ALARM")
        top = min(self.primary_tiles.max_zoom, self.fallback_tiles.max_zoom)
        if self.node_detail_zoom > top:
            raise ValueError(f"node_detail_zoom {self.node_detail_zoom} is past the tiles' max_zoom {top}")
        return self


class FeaturesConfig(_Strict):
    # Later project phases. False = panel shown greyed "NOT CONNECTED".
    thermal_camera: bool
    drone: bool
    wind_station: bool
    # Demo only: on a fire alarm the console shows a SIMULATED drone flying
    # from its base to the node, clearly labelled as a simulation.
    drone_simulation: bool = False


class ConsoleConfig(_Strict):
    title: str
    subtitle: str
    map: MapConfig
    features: FeaturesConfig


class Config(_Strict):
    demo_layout: DemoLayout | None = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_layout(cls, raw):
        return _apply_demo_layout(raw) if isinstance(raw, dict) else raw


    mqtt: MqttConfig
    database: DatabaseConfig
    timing: TimingConfig
    lorawan: LorawanConfig
    simulator: SimulatorConfig
    detection: DetectionConfig
    notifiers: NotifiersConfig
    web: WebConfig
    sites: SitesConfig
    console: ConsoleConfig


def _apply_env_overrides(raw: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    for var, (section, key) in ENV_OVERRIDES.items():
        value = env.get(var)
        if value:
            raw.setdefault(section, {})[key] = value
    return raw


def _derive_offline_threshold(raw: dict[str, Any]) -> dict[str, Any]:
    """detection.offline_after_minutes comes from timing, never from the YAML,
    so "missing" in the detector and "missing" in the UI cannot disagree."""
    det = raw.get("detection")
    if not isinstance(det, dict):
        return raw  # validation will report it
    if "offline_after_minutes" in det:
        raise ValueError(
            "detection.offline_after_minutes is derived from timing.heartbeat_minutes x "
            "timing.missed_heartbeats_for_missing; remove it from the config")
    timing = raw.get("timing") or {}
    try:
        det["offline_after_minutes"] = (
            float(timing["heartbeat_minutes"]) * int(timing["missed_heartbeats_for_missing"]))
    except (KeyError, TypeError, ValueError):
        pass  # validation of the timing section reports the real problem
    return raw


def apply_speedup(cfg: Config) -> Config:
    """Divide every wall-clock duration by timing.demo_speedup.

    Scaled: heartbeat (and so the missing threshold), baseline half-lives,
    baseline freeze limit, notification cooldown, startup replay window,
    smoke ramp/hold, simulated firmware timings, default chart window.
    (The duty-cycle gap is computed from airtime; the simulator divides it.)
    Not scaled: sample counts (warm-up, confirmation), thresholds, and the
    simulator's weather and day/night cycle, which follow the real clock.
    """
    k = cfg.timing.demo_speedup
    if k == 1:
        return cfg
    t, d, s = cfg.timing, cfg.detection, cfg.simulator
    hl = d.baseline_half_life_minutes
    fw = s.firmware
    return cfg.model_copy(update={
        "timing": t.model_copy(update={"heartbeat_minutes": t.heartbeat_minutes / k,
                                       "system_check_minutes": t.system_check_minutes / k}),
        "detection": d.model_copy(update={
            "baseline_half_life_minutes": hl.model_copy(update={
                "gas": hl.gas / k, "temperature": hl.temperature / k, "humidity": hl.humidity / k}),
            "replay_hours_on_startup": d.replay_hours_on_startup / k,
            "max_baseline_freeze_minutes": d.max_baseline_freeze_minutes / k,
            "notify_cooldown_minutes": d.notify_cooldown_minutes / k,
            "offline_after_minutes": d.offline_after_minutes / k,
            "explicit_heartbeat_minutes": d.explicit_heartbeat_minutes / k,
        }),
        "simulator": s.model_copy(update={
            "smoke": s.smoke.model_copy(update={
                "ramp_minutes": s.smoke.ramp_minutes / k, "hold_minutes": s.smoke.hold_minutes / k}),
            "firmware": fw.model_copy(update={
                "sample_seconds": fw.sample_seconds / k,
                "interval_while_triggered_seconds": fw.interval_while_triggered_seconds / k,
                "interval_after_trigger_minutes": fw.interval_after_trigger_minutes / k,
                "alarm_hold_minutes": fw.alarm_hold_minutes / k,
                "local_trigger": fw.local_trigger.model_copy(update={
                    "baseline_half_life_minutes": fw.local_trigger.baseline_half_life_minutes / k}),
            }),
        }),
        "web": cfg.web.model_copy(update={"chart_hours": cfg.web.chart_hours / k}),
    })


def _apply_demo_layout(raw: dict[str, Any]) -> dict[str, Any]:
    """Resolve demo positions once; legacy explicit-coordinate configs still work.

    Sort by stable devEUI so YAML ordering cannot move existing identities.
    A node with both lat and lon in config.yaml keeps them: that is its real
    GPS position (surveyed). Every other node gets its planned grid cell.
    """
    if raw.get("demo_layout") is None:
        return raw
    from gateway.geometry import destination, grid_positions, offset_position

    layout = DemoLayout.model_validate(raw["demo_layout"])
    sites = raw["sites"]
    nodes = sites["nodes"]
    ids = [_hex(n["dev_eui"], 16, "dev_eui") for n in nodes]
    _unique(ids, "site node dev_eui")
    gws = sites["gateways"]
    if len(nodes) != 9 or not gws:
        raise ValueError("demo_layout requires exactly nine nodes and at least one gateway")
    sim_ids = {n["dev_eui"].lower() for n in raw["simulator"]["nodes"]}
    if set(ids) != sim_ids:
        raise ValueError("demo_layout site and simulator node identities must match")
    sim_gw = raw["simulator"]["gateway_id"].lower()
    if sim_gw not in {g["gateway_id"].lower() for g in gws}:
        raise ValueError("demo_layout: the simulator's gateway must be one of the site gateways")
    lat, lon = layout.centre.lat, layout.centre.lon
    for node, position in zip(sorted(nodes, key=lambda n: n["dev_eui"].lower()), grid_positions(lat, lon)):
        if "lat" in node and "lon" in node:
            _Located.model_validate({"lat": node["lat"], "lon": node["lon"]})
            node["surveyed"] = True
        elif "lat" in node or "lon" in node:
            raise ValueError(f"site node {node.get('label')}: give both lat and lon, or neither")
        else:
            node.update(position)
    for g in gws:
        if "lat" in g and "lon" in g:
            continue
        if len(gws) == 1 and layout.gateway_distance_m is not None:
            g.update(destination(lat, lon, layout.gateway_distance_m, layout.gateway_bearing_deg))
        else:
            raise ValueError(f"gateway {g.get('label')}: give its lat and lon (or, with a single gateway, "
                             "set demo_layout.gateway_distance_m and gateway_bearing_deg)")
    # The simulator sends every uplink through one gateway, so the pilot's
    # nodes belong to it unless config.yaml says otherwise.
    for node in nodes:
        node.setdefault("gateway_id", sim_gw)
    for key, where in (("drone", layout.drone), ("centre", layout.control_unit)):
        if isinstance(where, DemoOffset):
            sites[key].update(offset_position(lat, lon, where.east_m, where.north_m))
        else:
            sites[key].update(lat=where.lat, lon=where.lon)
    raw["console"]["map"].update(centre_lat=lat, centre_lon=lon)
    return raw


def load_config(path: str | Path | None = None, env: dict[str, str] | None = None) -> Config:
    """Load config from `path`, else $WILDFIRE_CONFIG, else ./config.yaml.
    Returns the EFFECTIVE config (demo speed-up already applied)."""
    env = dict(os.environ) if env is None else env
    path = Path(path or env.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    raw = _derive_offline_threshold(_apply_env_overrides(raw, env))
    return apply_speedup(Config.model_validate(raw))


def _duration(minutes: float) -> str:
    if minutes < 2:
        return f"{minutes * 60:.0f} s"
    if minutes < 120:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h"


def summary(cfg: Config) -> str:
    """Human-readable effective settings (what the processes will actually use)."""
    t, d = cfg.timing, cfg.detection
    sim_euis = {n.dev_eui for n in cfg.simulator.nodes}
    site_euis = {n.dev_eui for n in cfg.sites.nodes}
    lines = [
        f"demo speed-up       x{t.demo_speedup:g}" + ("  (real time)" if t.demo_speedup == 1 else
                                                     "  (DEMO: durations divided)"),
        f"heartbeat           every {_duration(t.heartbeat_minutes)}",
        f"node alarm mode     every {_duration(cfg.simulator.firmware.interval_while_triggered_seconds / 60)} "
        f"while triggered, then every {_duration(cfg.simulator.firmware.interval_after_trigger_minutes)} "
        f"for {_duration(cfg.simulator.firmware.alarm_hold_minutes)} "
        f"(simulated firmware; duty cycle permitting)",
        f"node MISSING after  {_duration(t.missing_after_minutes)} "
        f"({t.missed_heartbeats_for_missing} missed heartbeats)",
        f"warm-up             {d.warmup_samples} readings "
        f"(~{_duration(d.warmup_samples * t.heartbeat_minutes)} at heartbeat rate)",
        f"baseline half-life  gas {_duration(d.baseline_half_life_minutes.gas)}, "
        f"temp {_duration(d.baseline_half_life_minutes.temperature)}, "
        f"RH {_duration(d.baseline_half_life_minutes.humidity)}",
        f"notify cooldown     {_duration(d.notify_cooldown_minutes)}",
        f"system check log    every {_duration(t.system_check_minutes)}",
        "",
        f"sites               {len(cfg.sites.nodes)} nodes, {len(cfg.sites.gateways)} gateways, "
        f"centre: {cfg.sites.centre.label}",
    ]
    if cfg.simulator.reporting_mode == "event_driven":
        lines[1:5] = [
            f"heartbeat           every {_duration(d.explicit_heartbeat_minutes)} (event-driven)",
            "fire alerts         one per local ALARM episode; no routine telemetry",
            f"node MISSING after  {_duration(d.explicit_missing_minutes)} ({d.explicit_missed_heartbeats} intervals)",
            f"local sampling      every {cfg.simulator.firmware.sample_seconds:g} s; warm-up {d.warmup_samples} samples",
        ]
    blocks: dict[str, list[str]] = {}
    for n in cfg.sites.nodes:
        blocks.setdefault(n.block, []).append(n.label)
    for block, labels in sorted(blocks.items()):
        labels.sort()
        span = labels[0] if len(labels) == 1 else f"{labels[0]} .. {labels[-1]}"
        lines.append(f"  block {block:<8} {len(labels):>4} node{'s' * (len(labels) != 1)}  ({span})")
    if sim_euis - site_euis:
        lines.append(f"  WARNING: simulator nodes without a site entry: {sorted(sim_euis - site_euis)}")
    m, f = cfg.console.map, cfg.console.features
    if cfg.demo_layout:
        detail = min(max(m.node_detail_zoom, 18), m.primary_tiles.max_zoom, m.fallback_tiles.max_zoom)
        map_description = (f"map detail          demo grid: individual nodes from zoom {detail}; "
                           "overview: network summary and active alarms")
    else:
        map_description = (f"map detail          below zoom {m.node_detail_zoom} (default view {m.zoom}) only "
                           f"ALARM nodes; all nodes from {m.node_detail_zoom}, clustered within {m.cluster_radius_px} px")
    lines += ["", map_description,
              "phase-2 panels      " + ", ".join(
        f"{name} {'ON' if on else 'off'}" for name, on in
        (("thermal camera", f.thermal_camera), ("drone", f.drone), ("wind station", f.wind_station)))]
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary(load_config()))
