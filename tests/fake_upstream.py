"""Fake SOCKS5 Upstream for integration tests at the Tunneling seam.

Listens on loopback with an ephemeral port, performs the SOCKS5 handshake,
records every CONNECT request (so tests can assert the remote-DNS wire
bytes), and relays the Tunnel to a local target standing in for the remote
host. Any requested hostname is mapped to that target, which is what makes
ATYP=3 (remote DNS) observable: the name never resolves on this machine.
"""

import socket
import struct
import threading
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True)
class SocksRequest:
    """One SOCKS5 CONNECT request as received on the wire."""

    methods: bytes
    command: int
    atyp: int
    host: bytes
    port: int


def _relay(src: socket.socket, dst: socket.socket) -> None:
    """Independent byte relay with half-close propagation (test-side only)."""
    try:
        while True:
            data = src.recv(65536)
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


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("client closed during SOCKS5 handshake")
        data += chunk
    return data


class FakeUpstream:
    """Context manager: a SOCKS5 Upstream that carries Tunnels to ``target``."""

    def __init__(self, target: tuple[str, int]) -> None:
        self._target = target
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.requests: list[SocksRequest] = []
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def address(self) -> tuple[str, int]:
        return self._listener.getsockname()

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._listener.close()
        with self._lock:
            sockets = list(self._sockets)
        for s in sockets:
            try:
                s.close()
            except OSError:
                pass

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self._sockets.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            version, nmethods = _recv_exact(conn, 2)
            if version != 5:
                return
            methods = _recv_exact(conn, nmethods)
            if b"\x00" not in methods:
                conn.sendall(b"\x05\xff")  # no acceptable method
                return
            conn.sendall(b"\x05\x00")  # accept no-auth
            head = _recv_exact(conn, 4)
            ver, cmd, _rsv, atyp = head
            if ver != 5 or cmd != 1:
                conn.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
                return
            if atyp == 1:
                host = _recv_exact(conn, 4)
            elif atyp == 3:
                host = _recv_exact(conn, _recv_exact(conn, 1)[0])
            elif atyp == 4:
                host = _recv_exact(conn, 16)
            else:
                conn.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
                return
            port = struct.unpack(">H", _recv_exact(conn, 2))[0]
            with self._lock:
                self.requests.append(
                    SocksRequest(methods=methods, command=cmd, atyp=atyp,
                                 host=host, port=port)
                )
            target = socket.create_connection(self._target, timeout=10)
            target.settimeout(None)
            with self._lock:
                self._sockets.append(target)
            conn.sendall(
                b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1")
                + struct.pack(">H", 0)
            )
            t1 = threading.Thread(target=_relay, args=(conn, target), daemon=True)
            t2 = threading.Thread(target=_relay, args=(target, conn), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        except OSError:
            pass
        finally:
            conn.close()
