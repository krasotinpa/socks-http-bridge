"""Tunneling core: HTTP client connections carried through the SOCKS5 Upstream.

Ported from the proven prototype (git history) with behaviour preserved: CONNECT Tunnels
plus plain-HTTP forwarding, SOCKS5 with remote DNS (ATYP=3), TCP_NODELAY,
half-close propagation, and no idle timeout on established Tunnels
(long-lived SSE/WebSocket streams are first-class). Stdlib only.
"""

import logging
import socket
import struct
import threading
from urllib.parse import urlsplit

BUF = 65536
log = logging.getLogger("bridge")


def recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("upstream closed during SOCKS5 handshake")
        data += chunk
    return data


def socks5_connect(socks_host: str, socks_port: int, dst_host: str, dst_port: int,
                   timeout: float = 25) -> socket.socket:
    """Open a TCP connection to dst_host:dst_port through the SOCKS5 Upstream.
    Domain names are sent as-is (ATYP=3) so DNS resolves on the VPN side."""
    s = socket.create_connection((socks_host, socks_port), timeout=timeout)
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(b"\x05\x01\x00")  # version 5, 1 method, no-auth
        if recv_exact(s, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS5 proxy rejected no-auth method")
        try:
            hostb = dst_host.encode("idna")
        except UnicodeError:
            hostb = dst_host.encode("ascii")
        if len(hostb) > 255:
            raise ConnectionError("hostname too long for SOCKS5")
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(hostb)]) + hostb
                  + struct.pack(">H", dst_port))
        head = recv_exact(s, 4)
        if head[1] != 0:
            raise ConnectionError(f"SOCKS5 connect failed, rep={head[1]}")
        atyp = head[3]
        if atyp == 1:
            recv_exact(s, 4)
        elif atyp == 3:
            recv_exact(s, recv_exact(s, 1)[0])
        elif atyp == 4:
            recv_exact(s, 16)
        recv_exact(s, 2)  # bound port
        s.settimeout(None)  # long-lived streams (SSE / websocket) must not time out
        return s
    except Exception:
        s.close()
        raise


def pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(BUF)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def read_headers(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(BUF)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            raise ConnectionError("client headers too large")
    return data


def handle(client: socket.socket, addr: tuple[str, int],
           socks_host: str, socks_port: int) -> None:
    remote: socket.socket | None = None
    try:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.settimeout(30)
        raw = read_headers(client)
        if not raw:
            return
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split()
        if len(parts) < 3:
            return
        method, target = parts[0], parts[1]

        if method.upper() == "CONNECT":
            host, _, port_text = target.rpartition(":")
            port = int(port_text or 443)
            remote = socks5_connect(socks_host, socks_port, host, port)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            log.info("CONNECT %s:%s", host, port)
        else:
            url = urlsplit(target)
            url_host = url.hostname
            if not url_host:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            port = url.port or 80
            remote = socks5_connect(socks_host, socks_port, url_host, port)
            path = url.path or "/"
            if url.query:
                path += "?" + url.query
            fwd = [f"{method} {path} {parts[2]}"]
            for line in lines[1:]:
                name = line.split(":", 1)[0].strip().lower()
                if name in ("proxy-connection", "proxy-authenticate",
                            "proxy-authorization"):
                    continue
                fwd.append(line)
            remote.sendall(("\r\n".join(fwd) + "\r\n\r\n").encode("latin-1")
                           + body)
            log.info("%s %s://%s%s", method, url.scheme, url_host, path)

        client.settimeout(None)
        t1 = threading.Thread(target=pipe, args=(client, remote), daemon=True)
        t2 = threading.Thread(target=pipe, args=(remote, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception as exc:  # noqa: BLE001 - any failure must yield 502, never crash
        log.warning("%s -> %s", addr[0], exc)
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except OSError:
            pass
    finally:
        for s in (client, remote):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
