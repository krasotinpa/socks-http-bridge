"""Health: the periodic TCP-connect Probe of the Upstream feeds Bridge State.

Bridge State is Running / Stopped / Error (GLOSSARY.md). The Tray Shell
observes it through the ``state`` property and ``subscribe`` transition
callbacks, never through internals. Stdlib only.
"""

import enum
import logging
import socket
import threading
from collections.abc import Callable

log = logging.getLogger("bridge")

PROBE_TIMEOUT = 2.0  # a Probe must never block the loop for long


class BridgeState(enum.StrEnum):
    RUNNING = "Running"
    STOPPED = "Stopped"
    ERROR = "Error"


class BridgeHealth:
    """Bridge State driven by the Probe and by Listener bind results.

    Starts Stopped; ``start`` launches the Probe (first check immediate,
    then every ``probe_interval`` seconds). ``stop`` is the explicit user
    stop: Probe results no longer change the state. A Listener that failed
    to take its port keeps the Bridge in Error even while Probes succeed,
    until the Listener binds again or the Bridge restarts.
    """

    def __init__(self, upstream: tuple[str, int],
                 probe_interval: float = 10.0) -> None:
        self._upstream = upstream
        self._probe_interval = probe_interval
        self._state = BridgeState.STOPPED
        self._listener_failed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribers: list[Callable[[BridgeState, BridgeState], None]] = []

    @property
    def state(self) -> BridgeState:
        with self._lock:
            return self._state

    def subscribe(self, callback: Callable[[BridgeState, BridgeState], None]) -> None:
        """Register callback(old, new), fired on every actual transition.

        Called from the Probe thread; the Tray Shell must marshal to its own
        loop. Callbacks run outside the state lock, so they may read state.
        Ordering contract: transitions are serialized by the lock, but under
        thread races callback delivery order may differ from transition
        order. An event is a hint — read ``state`` for the current value.
        """
        with self._lock:
            self._subscribers.append(callback)

    def start(self) -> None:
        """Start the periodic Probe; state follows Upstream reachability.

        A repeated start() with a live Probe is a pure no-op. On an actual
        (re)start Listener failures are reset — a new start is a new
        attempt, and a caller that rebinds Listeners reports fresh failures
        via listener_failed(). After stop(), waits for the old Probe to
        exit (bounded by the Probe timeout) before starting the new one.
        """
        thread = self._thread
        if thread is not None and thread.is_alive():
            if not self._stop.is_set():
                return  # Probe already running: no state change at all
            thread.join(PROBE_TIMEOUT + 1.0)
        with self._lock:
            self._listener_failed = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._probe_loop, daemon=True,
                                        name="probe")
        self._thread.start()

    def stop(self) -> None:
        """User stop: Stopped, and an in-flight Probe cannot flip it."""
        self._stop.set()
        self._set_state(BridgeState.STOPPED)

    def listener_failed(self) -> None:
        """A Listener failed to take its port: Error until it binds again
        or the Bridge restarts."""
        with self._lock:
            self._listener_failed = True
        self._set_state(BridgeState.ERROR)

    def listener_recovered(self) -> None:
        """Every Listener bound again: clear the failure latch; the next
        successful Probe restores Running."""
        with self._lock:
            self._listener_failed = False

    def _probe_loop(self) -> None:
        while not self._stop.is_set():
            reachable = self._probe_once()
            self._set_state(BridgeState.RUNNING if reachable else BridgeState.ERROR,
                            from_probe=True)
            if self._stop.wait(self._probe_interval):
                return

    def _probe_once(self) -> bool:
        try:
            with socket.create_connection(self._upstream, timeout=PROBE_TIMEOUT):
                return True
        except OSError:
            return False

    def _set_state(self, new: BridgeState, *, from_probe: bool = False) -> None:
        with self._lock:
            if from_probe:
                if self._stop.is_set():
                    return  # user stop wins over an in-flight Probe
                if self._listener_failed:
                    new = BridgeState.ERROR
            old = self._state
            if old is new:
                return  # events only on actual transitions
            self._state = new
            subscribers = tuple(self._subscribers)
        log.info("Bridge State: %s -> %s", old, new)
        for callback in subscribers:
            callback(old, new)
