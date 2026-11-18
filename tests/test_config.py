"""Config seam tests: load/save/validate against tmp_path files only.

The real %LOCALAPPDATA%\\socks-http-bridge\\config.json is never touched —
a live Bridge may own that file.
"""

import json
from pathlib import Path

import pytest

from socks_http_bridge.config import Config, ConfigError


def write_config(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "config.json")
    assert cfg == Config()
    assert cfg.listeners == ["127.0.0.1:8118"]
    assert cfg.upstream == "127.0.0.1:9090"
    assert cfg.autostart is True
    assert cfg.probe_interval == 10
    assert cfg.log_path == "%LOCALAPPDATA%\\socks-http-bridge\\bridge.log"
    assert cfg.test_url == "https://ipinfo.io/json"


def test_save_load_round_trip(tmp_path: Path) -> None:
    cfg = Config(
        listeners=["127.0.0.1:8118", "127.0.0.1:8119"],
        upstream="127.0.0.1:1080",
        autostart=False,
        probe_interval=30,
        log_path=str(tmp_path / "logs" / "bridge.log"),
        test_url="https://example.test/ip",
    )
    path = tmp_path / "nested" / "config.json"
    cfg.save(path)  # parent dirs are created
    assert Config.load(path) == cfg


def test_partial_file_fills_defaults(tmp_path: Path) -> None:
    path = write_config(tmp_path / "config.json", {"probe_interval": 60})
    cfg = Config.load(path)
    assert cfg.probe_interval == 60
    assert cfg.listeners == Config().listeners
    assert cfg.upstream == Config().upstream


@pytest.mark.parametrize(("override", "match"), [
    ({"listeners": []}, "listeners"),
    ({"listeners": "127.0.0.1:8118"}, "listeners"),
    ({"listeners": [8118]}, "listeners"),
    ({"listeners": ["127.0.0.1:8118", "127.0.0.1:8118"]}, "duplicate"),
    ({"listeners": ["127.0.0.1:0"]}, "port"),
    ({"listeners": ["127.0.0.1:65536"]}, "port"),
    ({"listeners": ["127.0.0.1:http"]}, "port"),
    ({"listeners": ["8118"]}, "host:port"),
    ({"upstream": "127.0.0.1:70000"}, "port"),
    ({"probe_interval": 1}, "probe_interval"),
    ({"probe_interval": 301}, "probe_interval"),
    ({"probe_interval": "10"}, "probe_interval"),
    ({"probe_interval": True}, "probe_interval"),
    ({"autostart": "yes"}, "autostart"),
    ({"log_path": ""}, "log_path"),
    ({"test_url": ""}, "test_url"),
])
def test_invalid_values_rejected_with_clear_message(
        tmp_path: Path, override: dict[str, object], match: str) -> None:
    path = write_config(tmp_path / "config.json", override)
    with pytest.raises(ConfigError, match=match):
        Config.load(path)


@pytest.mark.parametrize("interval", [2, 300])
def test_probe_interval_bounds_allowed(tmp_path: Path, interval: int) -> None:
    path = write_config(tmp_path / "config.json", {"probe_interval": interval})
    assert Config.load(path).probe_interval == interval


@pytest.mark.parametrize("text", ["{not json", "[]", '"just a string"'])
def test_malformed_config_file_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "config.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(path)


def test_save_rejects_invalid_config(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="listeners"):
        Config(listeners=[]).save(tmp_path / "config.json")
    assert not (tmp_path / "config.json").exists()
