"""Smoke tests for the headless CLI: ``python -m socks_http_bridge``.

Runs the real entry point as a subprocess with the reference CLI flags
(--listen/--socks/--log) on ephemeral ports through a fake Upstream; the
live Bridge on 127.0.0.1:8118 and the AdGuard Upstream on 127.0.0.1:9090
are never touched.
"""

import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fake_upstream import FakeUpstream
from targets import EchoTarget, RecordingHttpTarget
from test_tunneling import establish_connect_tunnel, read_http_head, recv_exact


@pytest.fixture
def start_cli_bridge(
    tmp_path: Path,
) -> Iterator[Callable[[tuple[str, int]], tuple[str, int]]]:
    """Factory: run the headless CLI pointed at an Upstream address.

    Reserves an ephemeral Listener port, launches the module with the
    reference flags, and waits until the Listener accepts connections.
    Returns the Listener address.
    """
    procs: list[subprocess.Popen[bytes]] = []

    def _start(upstream_address: tuple[str, int]) -> tuple[str, int]:
        reserve = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
        reserve.close()
        proc = subprocess.Popen(
            [sys.executable, "-m", "socks_http_bridge",
             "--listen", f"127.0.0.1:{port}",
             "--socks", f"{upstream_address[0]}:{upstream_address[1]}",
             "--log", str(tmp_path / "bridge.log")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert proc.poll() is None, "CLI process died before the Listener came up"
            try:
                probe = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            except OSError:
                time.sleep(0.05)
                continue
            probe.close()
            return ("127.0.0.1", port)
        raise AssertionError("CLI Listener did not come up in time")

    yield _start
    for proc in procs:
        proc.terminate()
        proc.wait(timeout=10)


def test_cli_connect_tunnel_relays_bytes(start_cli_bridge, bridge_client, tmp_path):
    with EchoTarget() as target, FakeUpstream(target.address) as upstream:
        listener = start_cli_bridge(upstream.address)
        client = bridge_client(listener)
        establish_connect_tunnel(client)

        client.sendall(b"cli-smoke")
        assert recv_exact(client, 9) == b"cli-smoke"

    # --log: the entry point logs to the given path.
    log_text = (tmp_path / "bridge.log").read_text(encoding="utf-8")
    assert "HTTP proxy on" in log_text


def test_cli_plain_http_forwarding(start_cli_bridge, bridge_client):
    with RecordingHttpTarget() as target, FakeUpstream(target.address) as upstream:
        listener = start_cli_bridge(upstream.address)
        client = bridge_client(listener)
        client.sendall(
            b"GET http://example.test/x HTTP/1.1\r\n"
            b"Host: example.test\r\n"
            b"Proxy-Connection: keep-alive\r\n"
            b"\r\n"
        )
        assert read_http_head(client).startswith(b"HTTP/1.1 200 OK\r\n")

        assert len(target.requests) == 1
        forwarded_head, _ = target.requests[0]
        assert forwarded_head.split(b"\r\n")[0] == b"GET /x HTTP/1.1"
        assert b"proxy-connection" not in forwarded_head.lower()
