"""Seam tests for Health: the Probe and Bridge State on real local sockets.

Every socket binds port 0 on loopback (or rebinds a port this file closed);
the live Bridge and the live Upstream are never touched. Probe intervals
are tiny so the suite stays fast.
"""

import socket
import threading
import time
from typing import Self

from socks_http_bridge.health import BridgeHealth, BridgeState


def _wait_state(health: BridgeHealth, expected: BridgeState,
                timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health.state is expected:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"Bridge State did not become {expected} (is {health.state})")


def _closed_port() -> int:
    """An ephemeral port nobody listens on: connects are refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CountingListener:
    """Test-local TCP listener that accepts and counts Probe connections."""

    def __init__(self, port: int = 0) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(128)
        self.address: tuple[str, int] = self._srv.getsockname()
        self.count = 0
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self.count += 1
            conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._srv.close()


def test_open_upstream_means_running() -> None:
    with _CountingListener() as upstream:
        health = BridgeHealth(upstream.address, probe_interval=0.05)
        transitions: list[tuple[BridgeState, BridgeState]] = []
        health.subscribe(lambda old, new: transitions.append((old, new)))
        health.start()
        try:
            _wait_state(health, BridgeState.RUNNING)
            assert transitions == [(BridgeState.STOPPED, BridgeState.RUNNING)]
        finally:
            health.stop()


def test_closed_upstream_means_error() -> None:
    health = BridgeHealth(("127.0.0.1", _closed_port()), probe_interval=0.05)
    health.start()
    try:
        _wait_state(health, BridgeState.ERROR)
    finally:
        health.stop()


def test_upstream_recovery_returns_to_running() -> None:
    port = _closed_port()
    health = BridgeHealth(("127.0.0.1", port), probe_interval=0.05)
    health.start()
    try:
        _wait_state(health, BridgeState.ERROR)
        with _CountingListener(port):
            _wait_state(health, BridgeState.RUNNING)
    finally:
        health.stop()


def test_user_stop_is_stopped_and_probe_does_not_alarm() -> None:
    health = BridgeHealth(("127.0.0.1", _closed_port()), probe_interval=0.05)
    transitions: list[tuple[BridgeState, BridgeState]] = []
    health.subscribe(lambda old, new: transitions.append((old, new)))
    health.start()
    _wait_state(health, BridgeState.ERROR)
    health.stop()
    assert health.state is BridgeState.STOPPED
    # Several Probe intervals pass with the Upstream still closed.
    time.sleep(0.3)
    assert health.state is BridgeState.STOPPED
    assert transitions == [(BridgeState.STOPPED, BridgeState.ERROR),
                           (BridgeState.ERROR, BridgeState.STOPPED)]


def test_probe_interval_respected_and_events_only_on_transitions() -> None:
    with _CountingListener() as upstream:
        health = BridgeHealth(upstream.address, probe_interval=0.1)
        transitions: list[tuple[BridgeState, BridgeState]] = []
        health.subscribe(lambda old, new: transitions.append((old, new)))
        health.start()
        try:
            _wait_state(health, BridgeState.RUNNING)
            time.sleep(0.5)
        finally:
            health.stop()
        # ~6 Probes in 0.5 s at a 0.1 s interval; a busy-loop yields hundreds.
        assert 3 <= upstream.count <= 10, upstream.count
        assert transitions == [(BridgeState.STOPPED, BridgeState.RUNNING),
                               (BridgeState.RUNNING, BridgeState.STOPPED)]


def test_listener_failure_sets_error_and_keeps_it() -> None:
    with _CountingListener() as upstream:
        health = BridgeHealth(upstream.address, probe_interval=0.05)
        transitions: list[tuple[BridgeState, BridgeState]] = []
        health.subscribe(lambda old, new: transitions.append((old, new)))
        health.start()
        try:
            _wait_state(health, BridgeState.RUNNING)
            health.listener_failed()
            assert health.state is BridgeState.ERROR
            # The Upstream keeps answering Probes, but a Listener that failed
            # to take its port keeps the Bridge in Error (see GLOSSARY.md).
            time.sleep(0.2)
            assert health.state is BridgeState.ERROR
            assert transitions == [(BridgeState.STOPPED, BridgeState.RUNNING),
                                   (BridgeState.RUNNING, BridgeState.ERROR)]
        finally:
            health.stop()


def test_double_start_keeps_a_single_probe() -> None:
    with _CountingListener() as upstream:
        health = BridgeHealth(upstream.address, probe_interval=0.2)
        health.start()
        try:
            _wait_state(health, BridgeState.RUNNING)
            health.start()  # idempotent: must not spawn a second Probe
            count_before = upstream.count
            time.sleep(1.0)
            delta = upstream.count - count_before
            # ~5 Probes in 1 s at a 0.2 s interval; two Probes would double it.
            assert 2 <= delta <= 7, delta
        finally:
            health.stop()


def test_start_clears_listener_failure() -> None:
    with _CountingListener() as upstream:
        health = BridgeHealth(upstream.address, probe_interval=0.05)
        health.start()
        _wait_state(health, BridgeState.RUNNING)
        health.listener_failed()
        assert health.state is BridgeState.ERROR
        health.stop()
        # A real restart is a new attempt: the caller rebound the Listeners
        # successfully, so the next successful Probe means Running again.
        health.start()
        try:
            _wait_state(health, BridgeState.RUNNING)
        finally:
            health.stop()


def test_noop_start_keeps_listener_failure_error() -> None:
    with _CountingListener() as upstream:
        health = BridgeHealth(upstream.address, probe_interval=0.05)
        health.start()
        try:
            _wait_state(health, BridgeState.RUNNING)
            health.listener_failed()
            assert health.state is BridgeState.ERROR
            # start() with a live Probe is a pure no-op: the sticky Error
            # survives successful Probes while the Listener is still down.
            health.start()
            time.sleep(0.2)
            assert health.state is BridgeState.ERROR
        finally:
            health.stop()


def test_partial_listener_failure_degrades_and_probes_keep_it() -> None:
    with _CountingListener() as upstream:
        health = BridgeHealth(upstream.address, probe_interval=0.05)
        transitions: list[tuple[BridgeState, BridgeState]] = []
        health.subscribe(lambda old, new: transitions.append((old, new)))
        health.start()
        try:
            _wait_state(health, BridgeState.RUNNING)
            health.listener_failed(partial=True)
            assert health.state is BridgeState.DEGRADED
            # Some Listeners still serve and the Upstream answers Probes:
            # Degraded holds, not Running and not Error (issue #65).
            time.sleep(0.2)
            assert health.state is BridgeState.DEGRADED
            assert transitions == [(BridgeState.STOPPED, BridgeState.RUNNING),
                                   (BridgeState.RUNNING, BridgeState.DEGRADED)]
        finally:
            health.stop()


def test_probe_failure_while_partial_reports_error() -> None:
    health = BridgeHealth(("127.0.0.1", _closed_port()), probe_interval=0.05)
    health.start()
    try:
        _wait_state(health, BridgeState.ERROR)
        health.listener_failed(partial=True)
        assert health.state is BridgeState.DEGRADED  # optimistic at Start
        _wait_state(health, BridgeState.ERROR)  # the next Probe corrects it
        time.sleep(0.15)
        assert health.state is BridgeState.ERROR  # red dominates
    finally:
        health.stop()


def test_listener_recovered_from_partial_restores_running() -> None:
    with _CountingListener() as upstream:
        health = BridgeHealth(upstream.address, probe_interval=0.05)
        health.start()
        try:
            _wait_state(health, BridgeState.RUNNING)
            health.listener_failed(partial=True)
            assert health.state is BridgeState.DEGRADED
            health.listener_recovered()
            _wait_state(health, BridgeState.RUNNING)
        finally:
            health.stop()


def test_noop_start_keeps_partial_degraded() -> None:
    with _CountingListener() as upstream:
        health = BridgeHealth(upstream.address, probe_interval=0.05)
        health.start()
        try:
            _wait_state(health, BridgeState.RUNNING)
            health.listener_failed(partial=True)
            health.start()  # pure no-op: the partial latch survives
            time.sleep(0.2)
            assert health.state is BridgeState.DEGRADED
        finally:
            health.stop()



def test_stop_then_start_resumes_probe() -> None:
    port = _closed_port()
    health = BridgeHealth(("127.0.0.1", port), probe_interval=0.05)
    health.start()
    _wait_state(health, BridgeState.ERROR)
    health.stop()
    assert health.state is BridgeState.STOPPED
    try:
        with _CountingListener(port):
            health.start()
            _wait_state(health, BridgeState.RUNNING)
    finally:
        health.stop()
