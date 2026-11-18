"""CLI integration tests for Config-driven headless startup.

Runs the real entry point as a subprocess with --config (or the default
Config path redirected via LOCALAPPDATA) on ephemeral ports through a fake
Upstream. The real %LOCALAPPDATA% Config, the live Bridge on 127.0.0.1:8118
and the AdGuard Upstream on 127.0.0.1:9090 are never touched.
"""

import io
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from fake_upstream import FakeUpstream
from targets import EchoTarget
from test_tunneling import establish_connect_tunnel, recv_exact


def _reserve_ports(count: int) -> list[int]:
    reserves = []
    for _ in range(count):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        reserves.append(s)
    ports = [s.getsockname()[1] for s in reserves]
    for s in reserves:
        s.close()
    return ports


def _wait_for_listener(proc: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert proc.poll() is None, "CLI process died before the Listener came up"
        try:
            probe = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        except OSError:
            time.sleep(0.05)
            continue
        probe.close()
        return
    raise AssertionError("CLI Listener did not come up in time")


@pytest.fixture
def start_cli_config_bridge(
    tmp_path: Path,
) -> Iterator[Callable[..., tuple[list[tuple[str, int]], Path]]]:
    """Factory: run the headless CLI from a Config file on ephemeral ports.

    Writes a Config with ``listener_count`` Listeners and the given Upstream,
    launches the module with --config (or with no flags and LOCALAPPDATA
    redirected to tmp_path when ``use_default_path``), and waits until every
    Listener accepts connections. Returns the Listener addresses and the
    Config log path (in a not-yet-existing nested dir).
    """
    procs: list[subprocess.Popen[bytes]] = []

    def _start(upstream_address: tuple[str, int], listener_count: int = 1,
               use_default_path: bool = False,
               ) -> tuple[list[tuple[str, int]], Path]:
        ports = _reserve_ports(listener_count)
        log_path = tmp_path / "nested" / "bridge.log"
        config = {
            "listeners": [f"127.0.0.1:{p}" for p in ports],
            "upstream": f"{upstream_address[0]}:{upstream_address[1]}",
            "log_path": str(log_path),
        }
        argv = [sys.executable, "-m", "socks_http_bridge"]
        env = None
        if use_default_path:
            config_dir = tmp_path / "socks-http-bridge"
            config_dir.mkdir()
            config_path = config_dir / "config.json"
            env = {**os.environ, "LOCALAPPDATA": str(tmp_path)}
        else:
            config_path = tmp_path / "config.json"
            argv += ["--config", str(config_path)]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env)
        procs.append(proc)
        for port in ports:
            _wait_for_listener(proc, port)
        return [("127.0.0.1", p) for p in ports], log_path

    yield _start
    for proc in procs:
        proc.terminate()
        proc.wait(timeout=10)


def test_cli_serves_all_listeners_from_config(
        start_cli_config_bridge, bridge_client) -> None:
    with EchoTarget() as target, FakeUpstream(target.address) as upstream:
        listeners, log_path = start_cli_config_bridge(upstream.address,
                                                      listener_count=2)
        assert len(listeners) == 2
        for listener in listeners:
            client = bridge_client(listener)
            establish_connect_tunnel(client)
            payload = b"multi-listener"
            client.sendall(payload)
            assert recv_exact(client, len(payload)) == payload
        # Log goes to Config log_path; parent dirs were created.
        assert "HTTP proxy on" in log_path.read_text(encoding="utf-8")


def test_cli_reads_default_config_path(
        start_cli_config_bridge, bridge_client) -> None:
    with EchoTarget() as target, FakeUpstream(target.address) as upstream:
        listeners, _ = start_cli_config_bridge(upstream.address,
                                               use_default_path=True)
        client = bridge_client(listeners[0])
        establish_connect_tunnel(client)
        payload = b"default-path"
        client.sendall(payload)
        assert recv_exact(client, len(payload)) == payload


@pytest.mark.parametrize("text", [
    json.dumps({"listeners": []}),
    json.dumps({"listeners": ["127.0.0.1:99999"]}),
    json.dumps({"probe_interval": 1}),
    "{broken json",
])
def test_cli_invalid_config_exits_with_readable_error(
        tmp_path: Path, text: str) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(text, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "socks_http_bridge", "--config", str(config_path)],
        capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode != 0
    assert "Config error" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_flags_override_config(tmp_path: Path, bridge_client) -> None:
    with EchoTarget() as target, FakeUpstream(target.address) as upstream:
        port = _reserve_ports(1)[0]
        config_path = tmp_path / "config.json"
        # Config values that must NOT be used: port 1 fails to bind, and the
        # Config log dir must never be created when --log overrides it.
        config_path.write_text(json.dumps({
            "listeners": ["127.0.0.1:1"],
            "upstream": "127.0.0.1:1",
            "log_path": str(tmp_path / "config-log" / "bridge.log"),
        }), encoding="utf-8")
        flag_log = tmp_path / "flag-log" / "bridge.log"
        proc = subprocess.Popen(
            [sys.executable, "-m", "socks_http_bridge",
             "--config", str(config_path),
             "--listen", f"127.0.0.1:{port}",
             "--socks", f"{upstream.address[0]}:{upstream.address[1]}",
             "--log", str(flag_log)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_for_listener(proc, port)
            client = bridge_client(("127.0.0.1", port))
            establish_connect_tunnel(client)
            payload = b"flag-override"
            client.sendall(payload)
            assert recv_exact(client, len(payload)) == payload
            assert "HTTP proxy on" in flag_log.read_text(encoding="utf-8")
            assert not (tmp_path / "config-log").exists()
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_logging_rotates_2mb_x2_and_creates_parent_dirs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from socks_http_bridge.__main__ import _setup_logging
    from socks_http_bridge.tunneling import log

    # Keep the ~60k probe records off the console when pytest runs with -s.
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    log_path = tmp_path / "deep" / "nested" / "bridge.log"
    _setup_logging(str(log_path), verbose=False)
    rotating = [h for h in log.handlers if isinstance(h, RotatingFileHandler)]
    try:
        assert len(rotating) == 1
        assert rotating[0].maxBytes == 2_000_000
        assert rotating[0].backupCount == 2
        assert log_path.parent.is_dir()

        # ~7.9 MB of records: three rollovers, so the oldest lines are
        # evicted past bridge.log.2 and each file holds a newer span.
        for i in range(60_000):
            log.info("rotation-probe %06d %s", i, "x" * 80)
        backup1 = log_path.with_name(log_path.name + ".1")
        backup2 = log_path.with_name(log_path.name + ".2")
        assert backup1.exists()
        assert backup2.exists()
        assert log_path.stat().st_size < 2_000_000

        def first_marker(path: Path) -> int:
            with path.open(encoding="utf-8") as f:
                line = f.readline()
            return int(line.split("rotation-probe ")[1][:6])

        assert first_marker(backup2) < first_marker(backup1) < first_marker(log_path)
        for path in (log_path, backup1, backup2):
            assert "rotation-probe 000000" not in path.read_text(encoding="utf-8")
    finally:
        for h in rotating:
            log.removeHandler(h)
            h.close()


@pytest.mark.parametrize(("flag", "value"), [
    ("--listen", "garbage"),
    ("--listen", "127.0.0.1:99999"),
    ("--socks", "garbage"),
    ("--socks", "127.0.0.1:0"),
])
def test_cli_invalid_flag_exits_with_readable_error(
        tmp_path: Path, flag: str, value: str) -> None:
    # A valid Config file isolates the failure to the flag itself.
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "socks_http_bridge",
         "--config", str(config_path), flag, value],
        capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode != 0
    assert "Config error" in proc.stderr
    assert flag in proc.stderr
    assert "Traceback" not in proc.stderr
