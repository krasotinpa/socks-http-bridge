"""Config: the persisted Bridge settings — load, save, validate.

Schema (config.json): listeners, upstream, autostart, probe_interval,
log_path, test_url. Autostart, Probe interval and Exit IP Test URL are
stored and validated here only; their behaviour belongs to later issues.
Stdlib only.
"""

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


class ConfigError(ValueError):
    """Invalid Config; the message is readable and safe to show the user."""


def default_config_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return Path(base) / "socks-http-bridge" / "config.json"


def parse_address(value: object, name: str) -> tuple[str, int]:
    """Split a host:port address, rejecting ports outside 1..65535."""
    if not isinstance(value, str):
        raise ConfigError(f"{name}: expected a host:port string, got {value!r}")
    host, sep, port_text = value.rpartition(":")
    if not sep or not host:
        raise ConfigError(f"{name}: {value!r} is not host:port")
    try:
        port = int(port_text)
    except ValueError:
        raise ConfigError(f"{name}: port in {value!r} is not an integer") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"{name}: port in {value!r} is outside 1..65535")
    return host, port


def connect_target(address: tuple[str, int]) -> tuple[str, int]:
    """Self-connect destination for a bound Listener address.

    Windows rejects connect() to a wildcard bind address (WinError 10049),
    so map wildcards to loopback; every other host stays as bound.
    """
    host, port = address
    return {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host), port


@dataclass
class Config:
    listeners: list[str] = field(default_factory=lambda: ["127.0.0.1:8118"])
    upstream: str = "127.0.0.1:9090"
    autostart: bool = True
    probe_interval: int = 10
    log_path: str = "%LOCALAPPDATA%\\socks-http-bridge\\bridge.log"
    test_url: str = "https://ipinfo.io/json"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Read Config from path (default: the per-user config.json).

        A missing file yields the defaults; anything unreadable or invalid
        raises ConfigError with a message meant for the user, not a traceback.
        """
        path = default_config_path() if path is None else path
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read Config {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"Config {path}: expected a JSON object")
        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg.validate()
        return cfg

    def save(self, path: Path | None = None) -> None:
        """Validate and write Config as JSON, creating parent dirs."""
        path = default_config_path() if path is None else path
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n",
                        encoding="utf-8")

    def validate(self) -> None:
        if not isinstance(self.listeners, list) or not self.listeners:
            raise ConfigError(
                "listeners must be a non-empty list of host:port addresses")
        # Dedup on the parsed address so 8118 and 08118 are one Listener.
        seen: set[tuple[str, int]] = set()
        for listener in self.listeners:
            address = parse_address(listener, "listeners")
            if address in seen:
                raise ConfigError(f"duplicate Listener {listener!r} in listeners")
            seen.add(address)
        parse_address(self.upstream, "upstream")
        if not isinstance(self.autostart, bool):
            raise ConfigError(f"autostart must be a boolean, got {self.autostart!r}")
        if (isinstance(self.probe_interval, bool)
                or not isinstance(self.probe_interval, int)
                or not 2 <= self.probe_interval <= 300):
            raise ConfigError("probe_interval must be an integer in 2..300, got "
                              f"{self.probe_interval!r}")
        if not isinstance(self.log_path, str) or not self.log_path:
            raise ConfigError("log_path must be a non-empty string")
        if not isinstance(self.test_url, str) or not self.test_url:
            raise ConfigError("test_url must be a non-empty string")
