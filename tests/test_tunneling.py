"""Integration tests at the Tunneling seam: real sockets, fake Upstream."""

import socket

from fake_upstream import FakeUpstream
from targets import EchoTarget, RecordingHttpTarget


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


def test_plain_http_forwarding_strips_proxy_hop_by_hop_headers(start_bridge,
                                                               bridge_client):
    with RecordingHttpTarget() as target, FakeUpstream(target.address) as upstream:
        listener = start_bridge(upstream.address)
        client = bridge_client(listener)
        client.sendall(
            b"POST http://example.test/some/path?q=1 HTTP/1.1\r\n"
            b"Host: example.test\r\n"
            b"Proxy-Connection: keep-alive\r\n"
            b"Proxy-Authenticate: Basic realm=\"hop\"\r\n"
            b"Proxy-Authorization: Basic c2VjcmV0\r\n"
            b"Connection: keep-alive\r\n"
            b"X-Custom: preserved\r\n"
            b"Content-Length: 11\r\n"
            b"\r\n"
            b"body-bytes!"
        )
        raw = read_http_head(client)
        head, _, rest = raw.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK\r\n")
        while len(rest) < 12:
            chunk = client.recv(65536)
            assert chunk, "connection closed before the response body arrived"
            rest += chunk
        assert rest == b"forwarded-ok"

        assert len(target.requests) == 1
        forwarded_head, forwarded_body = target.requests[0]
        lines = forwarded_head.decode("latin-1").split("\r\n")
        assert lines[0] == "POST /some/path?q=1 HTTP/1.1"  # origin-form target
        names = [line.split(":", 1)[0].strip().lower() for line in lines[1:]]
        assert "proxy-connection" not in names
        assert "proxy-authenticate" not in names
        assert "proxy-authorization" not in names
        # Only proxy hop-by-hop headers are stripped; the rest pass through.
        assert "host" in names
        assert "connection" in names
        assert "x-custom" in names
        assert forwarded_body == b"body-bytes!"
        assert upstream.requests[0].host == b"example.test"


def test_socks5_request_sends_hostname_as_atyp3_for_remote_dns(start_bridge,
                                                               bridge_client):
    # A name that does not resolve on this machine: the Tunnel can only work
    # if the hostname travels to the Upstream as-is (ATYP=3, remote DNS).
    name = "remote-dns-only.example.test"
    with EchoTarget() as target, FakeUpstream(target.address) as upstream:
        listener = start_bridge(upstream.address)
        client = bridge_client(listener)
        establish_connect_tunnel(client, host=name, port=8443)

        assert len(upstream.requests) == 1
        request = upstream.requests[0]
        assert request.command == 1  # CONNECT
        assert request.atyp == 3  # domain name, not a pre-resolved IP
        assert request.host == name.encode("ascii")
        assert request.port == 8443
        assert b"\x00" in request.methods  # no-auth offered

        client.sendall(b"remote-dns works")
        assert recv_exact(client, 16) == b"remote-dns works"