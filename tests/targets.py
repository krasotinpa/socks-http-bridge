"""Local stand-ins for the remote host at the far end of a Tunnel.

Each target listens on loopback with an ephemeral port and serves one
handler thread per connection, like a real remote host would.
"""

import socket
import threading
import time

from loopback import LoopbackServer


class _Target(LoopbackServer):
    """Base for local stand-ins: a loopback server playing the remote host."""


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


class SilentTarget(_Target):
    """Accepts the request, then never replies: drives client read timeouts.

    The wait is longer than the small explicit timeout the test passes, but
    the socket is tracked so ``stop`` closes it and the handler thread exits.
    """

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.recv(65536)  # read the request head, then go silent
            time.sleep(30)
        except OSError:
            pass
        finally:
            conn.close()


class RecordingHttpTarget(_Target):
    """Minimal HTTP origin: records the forwarded request, replies 200
    with a canned payload (default keeps the original forwarding marker)."""

    def __init__(self, payload: bytes = b"forwarded-ok") -> None:
        super().__init__()
        self._payload = payload
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
            payload = self._payload
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
