"""CLI integration tests for Health: Listener bind failure and Bridge State.

Runs the real entry point as a subprocess on ephemeral ports; one Listener
port is deliberately taken by a socket this file holds. The real Config,
the live Bridge and the live Upstream are never touched.
"""

import json
import socket
import subprocess
import sys
from pathlib import Path

from fake_upstream import FakeUpstream
from targets import EchoTarget
from test_cli_config import _wait_for_listener
from test_tunneling import establish_connect_tunnel, recv_exact


def test_cli_bind_failure_logs_error_and_other_listeners_serve(
        tmp_path: Path, bridge_client) -> None:
    reserves = [socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                for _ in range(2)]
    for s in reserves:
        s.bind(("127.0.0.1", 0))
    taken_port = reserves[0].getsockname()[1]  # kept bound: the port is taken
    free_port = reserves[1].getsockname()[1]
    reserves[1].close()
    log_path = tmp_path / "bridge.log"
    with EchoTarget() as target, FakeUpstream(target.address) as upstream:
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "listeners": [f"127.0.0.1:{taken_port}", f"127.0.0.1:{free_port}"],
            "upstream": f"{upstream.address[0]}:{upstream.address[1]}",
            "log_path": str(log_path),
        }), encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "socks_http_bridge",
             "--config", str(config_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_for_listener(proc, free_port)
            client = bridge_client(("127.0.0.1", free_port))
            establish_connect_tunnel(client)
            payload = b"bind-failure"
            client.sendall(payload)
            assert recv_exact(client, len(payload)) == payload
            log_text = log_path.read_text(encoding="utf-8")
            assert "failed to bind" in log_text
            assert f"127.0.0.1:{taken_port}" in log_text
            # Error from the bind failure; the healthy Upstream Probe must
            # not flap it back to Running while the Listener is still down.
            # (The first Probe may land before or after the bind failure.)
            assert "-> Error" in log_text
            assert "Error -> Running" not in log_text
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            reserves[0].close()


def test_cli_all_listeners_fail_exits_nonzero(tmp_path: Path) -> None:
    taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    taken.bind(("127.0.0.1", 0))
    taken_port = taken.getsockname()[1]
    log_path = tmp_path / "bridge.log"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "listeners": [f"127.0.0.1:{taken_port}"],
        "log_path": str(log_path),
    }), encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "socks_http_bridge",
             "--config", str(config_path)],
            capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode != 0
        assert "Traceback" not in proc.stderr
        log_text = log_path.read_text(encoding="utf-8")
        assert "failed to bind" in log_text
        assert "No Listener could take its port" in log_text
    finally:
        taken.close()
