"""Integration tests at the Tunneling seam: real sockets, fake Upstream."""

import socket

from fake_upstream import FakeUpstream
from targets import EchoTarget


def recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        assert chunk, "connection closed before all expected bytes arrived"
        data += chunk
    return data


def read_http_head(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


def establish_connect_tunnel(client: socket.socket,
                             host: str = "example.test", port: int = 443) -> None:
    client.sendall(
        f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii")
    )
    assert read_http_head(client) == b"HTTP/1.1 200 Connection established\r\n\r\n"


def test_connect_tunnel_relays_bytes_in_both_directions(start_bridge, bridge_client):
    with EchoTarget() as target, FakeUpstream(target.address) as upstream:
        listener = start_bridge(upstream.address)
        client = bridge_client(listener)
        establish_connect_tunnel(client)

        client.sendall(b"ping-1")
        assert recv_exact(client, 6) == b"ping-1"

        payload = bytes(range(256)) * 512  # 128 KiB, spans many recv chunks
        client.sendall(payload)
        assert recv_exact(client, len(payload)) == payload

        client.sendall(b"ping-2")
        assert recv_exact(client, 6) == b"ping-2"