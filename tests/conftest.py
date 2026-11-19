"""Fixtures that run the Tunneling core on loopback with ephemeral ports.

The live Bridge and the live Upstream are never touched: every socket here
binds port 0.
"""

import socket
import threading
from collections.abc import Callable, Iterator

import pytest

from socks_http_bridge import tunneling


@pytest.fixture
def start_bridge() -> Iterator[Callable[[tuple[str, int]], tuple[str, int]]]:
    """Factory: start the core's accept loop pointed at an Upstream address.

    Runs the same arrangement as the headless entry (one thread per client,
    ``tunneling.handle`` per connection) on an ephemeral Listener port.
    Returns the Listener address.
    """
    listeners: list[socket.socket] = []

    def _start(upstream_address: tuple[str, int]) -> tuple[str, int]:
        socks_host, socks_port = upstream_address
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(128)
        listeners.append(srv)

        def _accept_loop() -> None:
            while True:
                try:
                    client, addr = srv.accept()
                except OSError:
                    return
                threading.Thread(
                    target=tunneling.handle,
                    args=(client, addr, socks_host, socks_port),
                    daemon=True,
                ).start()

        threading.Thread(target=_accept_loop, daemon=True).start()
        return srv.getsockname()

    yield _start
    for srv in listeners:
        srv.close()


@pytest.fixture
def bridge_client() -> Iterator[Callable[..., socket.socket]]:
    """Factory: connect an HTTP client socket to a Listener.

    A safety timeout keeps a broken Tunnel from hanging the suite; the idle
    timeout test raises it explicitly.
    """
    sockets: list[socket.socket] = []

    def _connect(address: tuple[str, int], timeout: float = 10.0) -> socket.socket:
        s = socket.create_connection(address, timeout=10)
        s.settimeout(timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sockets.append(s)
        return s

    yield _connect
    for s in sockets:
        try:
            s.close()
        except OSError:
            pass
