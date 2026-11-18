"""Shared loopback-server shape for the test stand-ins.

Binds an ephemeral port on loopback, serves one handler thread per
connection, and tracks accepted sockets under a lock so ``stop`` can
close them all. Used by the fake targets and the fake Upstream alike.
"""

import socket
import threading
from typing import Self


class LoopbackServer:
    """Context manager: loopback server on an ephemeral port, tracked sockets."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def address(self) -> tuple[str, int]:
        return self._sock.getsockname()

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._sock.close()
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
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self._sockets.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        raise NotImplementedError
