"""Local stand-ins for the remote host at the far end of a Tunnel.

Each target listens on loopback with an ephemeral port and serves one
handler thread per connection, like a real remote host would.
"""

import socket
import threading
from typing import Self


class _Target:
    """Base: loopback Listener on an ephemeral port with tracked sockets."""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
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
        raise NotImplementedError


class EchoTarget(_Target):
    """Echoes every chunk back immediately: proves both Tunnel directions."""

    def _handle(self, conn: socket.socket) -> None:
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            conn.close()


class EofMarkerTarget(_Target):
    """Reads until EOF, then reports what it saw and half-closes its side.

    The marker is sent only after the client's SHUT_WR has propagated through
    the whole Tunnel, so receiving it proves half-close propagation; the EOF
    that follows proves the remote half-close travels back to the client.
    """

    def _handle(self, conn: socket.socket) -> None:
        seen = b""
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                seen += data
            conn.sendall(b"eof-seen:" + seen)
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        finally:
            conn.close()


class RecordingHttpTarget(_Target):
    """Minimal HTTP origin: records the forwarded request, replies 200."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[bytes, bytes]] = []  # (head, body)
        self._requests_lock = threading.Lock()

    def _handle(self, conn: socket.socket) -> None:
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.decode("latin-1").split("\r\n")[1:]:
                name, _, value = line.partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            while len(body) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk
            with self._requests_lock:
                self.requests.append((head, body))
            payload = b"forwarded-ok"
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(payload)).encode("ascii")
                + b"\r\nConnection: close\r\n\r\n" + payload
            )
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        finally:
            conn.close()
