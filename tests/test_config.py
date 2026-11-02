from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gateway.config import DEFAULT_CONFIG_PATH, load_config


def test_shipped_config_loads():
    cfg = load_config(DEFAULT_CONFIG_PATH, env={})
    assert len(cfg.simulator.nodes) == 4
    assert cfg.detection.baseline_half_life_minutes.gas == 120
    assert cfg.timing.heartbeat_minutes == 10


def test_env_overrides_connection_settings():
    cfg = load_config(
        DEFAULT_CONFIG_PATH,
        env={"DATABASE_URL": "sqlite:///data/x.db", "MQTT_HOST": "broker", "MQTT_PORT": "1884"},
    )
    assert cfg.database.url == "sqlite:///data/x.db"
    assert cfg.mqtt.host == "broker"
    assert cfg.mqtt.port == 1884


def _write_modified(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_typo_in_threshold_is_rejected(tmp_path):
    def mutate(raw):
        raw["detection"]["watch"]["gas_drop_pcnt"] = raw["detection"]["watch"].pop("gas_drop_pct")

    with pytest.raises(ValidationError):
        load_config(_write_modified(tmp_path, mutate), env={})


def test_alarm_must_be_stricter_than_watch(tmp_path):
    def mutate(raw):
        raw["detection"]["alarm"]["gas_drop_pct"] = 10

    with pytest.raises(ValidationError, match="alarm threshold"):
        load_config(_write_modified(tmp_path, mutate), env={})


# --- timing: heartbeat, missing threshold, demo speed-up ---------------------

def test_offline_threshold_is_derived_from_heartbeat():
    cfg = load_config(DEFAULT_CONFIG_PATH, env={})
    assert cfg.timing.missing_after_minutes == 30
    # One definition of "missing", shared by the detector and the UI.
    assert cfg.detection.offline_after_minutes == cfg.timing.missing_after_minutes


def test_offline_threshold_in_yaml_is_rejected(tmp_path):
    def mutate(raw):
        raw["detection"]["offline_after_minutes"] = 10

    with pytest.raises(ValueError, match="derived"):
        load_config(_write_modified(tmp_path, mutate), env={})


def test_speedup_divides_durations_not_sample_counts():
    real = load_config(DEFAULT_CONFIG_PATH, env={})
    fast = load_config(DEFAULT_CONFIG_PATH, env={"DEMO_SPEEDUP": "60"})
    assert fast.timing.demo_speedup == 60
    assert fast.timing.heartbeat_minutes == pytest.approx(10 / 60)
    assert fast.timing.missing_after_minutes == pytest.approx(30 / 60)
    assert fast.detection.offline_after_minutes == pytest.approx(30 / 60)
    assert fast.detection.baseline_half_life_minutes.gas == pytest.approx(2)
    assert fast.detection.notify_cooldown_minutes == pytest.approx(0.5)
    assert fast.simulator.smoke.ramp_minutes == pytest.approx(real.simulator.smoke.ramp_minutes / 60)
    # Counts and thresholds are untouched: warm-up stays 30 readings.
    assert fast.detection.warmup_samples == real.detection.warmup_samples == 30
    assert fast.detection.confirm_samples == real.detection.confirm_samples
    assert fast.detection.alarm == real.detection.alarm


@pytest.mark.parametrize("value", [0, 0.5, 5000])
def test_speedup_out_of_range_is_rejected(tmp_path, value):
    def mutate(raw):
        raw["timing"]["demo_speedup"] = value

    with pytest.raises(ValidationError):
        load_config(_write_modified(tmp_path, mutate), env={})


# --- site registry --------------------------------------------------------------

def test_every_simulated_node_has_a_site():
    cfg = load_config(DEFAULT_CONFIG_PATH, env={})
    for n in cfg.simulator.nodes:
        site = cfg.sites.node(n.dev_eui)
        assert site is not None, n.name
        assert cfg.sites.label_for(n.dev_eui, n.name) == site.label
    assert cfg.sites.label_for("ffffffffffffffff", "fallback") == "fallback"


def test_duplicate_site_dev_eui_is_rejected(tmp_path):
    def mutate(raw):
        raw["sites"]["nodes"][1]["dev_eui"] = raw["sites"]["nodes"][0]["dev_eui"]

    with pytest.raises(ValidationError, match="duplicate"):
        load_config(_write_modified(tmp_path, mutate), env={})


def test_site_latitude_out_of_range_is_rejected(tmp_path):
    def mutate(raw):
        raw["sites"]["nodes"][0]["lat"] = 133.55

    with pytest.raises(ValidationError):
        load_config(_write_modified(tmp_path, mutate), env={})


def test_alarm_mode_port_must_differ_from_heartbeat_port(tmp_path):
    def mutate(raw):
        raw["lorawan"]["alarm_mode_f_port"] = raw["lorawan"]["heartbeat_f_port"]

    with pytest.raises(ValidationError, match="must differ"):
        load_config(_write_modified(tmp_path, mutate), env={})


# --- map level of detail --------------------------------------------------------

def test_default_view_shows_blocks_not_nodes():
    m = load_config(DEFAULT_CONFIG_PATH, env={}).console.map
    assert m.node_detail_zoom > m.zoom


@pytest.mark.parametrize("detail, match", [(15, "greater than zoom"), (14, "greater than zoom"),
                                           (20, "max_zoom")])
def test_node_detail_zoom_is_checked(tmp_path, detail, match):
    def mutate(raw):
        raw["console"]["map"]["node_detail_zoom"] = detail  # default view is zoom 15, tiles stop at 19

    with pytest.raises(ValidationError, match=match):
        load_config(_write_modified(tmp_path, mutate), env={})