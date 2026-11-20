"""Tray Shell: the Bridge lives in the Windows tray (GLOSSARY.md).

One pythonw process, icon coloured by Bridge State, hover tooltip with
per-Listener/Upstream status, menu with the status line, Stop/Start, Open
log and Exit. Observes Bridge State only through the
Health interface (``state`` + ``subscribe``); no UI code lives in Health or
Tunneling. Feature UIs are separate modules pre-wired into the menu:
exit_ip_ui (issue #6), settings (issue #7) and about_ui (issue #35).

Entry: ``pythonw.exe -m socks_http_bridge --tray`` (see __main__.py).
"""

import ctypes
import io
import os
import socket
import threading
from ctypes import wintypes
from importlib import resources

import pystray  # type: ignore[import-untyped]  # pystray ships no py.typed
from PIL import Image

from socks_http_bridge import about_ui, exit_ip_ui, settings
from socks_http_bridge.config import Config, parse_address
from socks_http_bridge.health import BridgeHealth, BridgeState
from socks_http_bridge.tunneling import handle, log

STATE_COLORS: dict[BridgeState, tuple[int, int, int]] = {
    BridgeState.RUNNING: (0, 160, 0),    # green
    BridgeState.STOPPED: (128, 128, 128),  # grey
    BridgeState.ERROR: (200, 0, 0),      # red
    BridgeState.DEGRADED: (230, 160, 0),  # amber
}

TIP_MAX = 127  # NOTIFYICONDATAW.szTip: 128 WCHARs incl. the terminator
DOT_OK = "\U0001F7E2"    # green circle
DOT_BAD = "\U0001F534"   # red circle
DOT_UNKNOWN = "\u26AA"   # white circle: not probed


def _tip_len(text: str) -> int:
    """Length in UTF-16 units: szTip counts WCHARs, emoji cost two."""
    return len(text.encode("utf-16-le")) // 2


def make_icon_image(state: BridgeState) -> Image.Image:
    """The bundled Bridge glyph PNG for this Bridge State (issue #45).

    Static assets in ``assets/`` (regenerate: scripts/make_icons.py);
    the tray never draws at runtime.
    """
    name = f"icon-{state.name.lower()}.png"
    data = (resources.files("socks_http_bridge")
            .joinpath("assets", name).read_bytes())
    return Image.open(io.BytesIO(data)).convert("RGBA")


def status_line(listeners: list[str], upstream: str) -> str:
    """``listen -> socks``: the first Listener plus a count when several."""
    extra = len(listeners) - 1
    suffix = f" (+{extra})" if extra > 0 else ""
    return f"{listeners[0]} -> {upstream}{suffix}"


class TrayShell:
    """Icon, menu and Listener lifecycle, driven by the Health interface.

    ``icon``/``health`` are injectable so tests run headless with fakes;
    production builds a real pystray.Icon and BridgeHealth. Feature modules
    reach the Bridge through ``config``, ``listener_addresses``, ``notify``
    and ``restart_listeners`` (Apply saves Config, then restarts Listeners
    atomically; Stop/Start share the same lifecycle lock).
    """

    def __init__(self, cfg: Config, addresses: list[tuple[str, int]],
                 upstream: tuple[str, int], log_path: str, *,
                 icon: pystray.Icon | None = None,
                 health: BridgeHealth | None = None) -> None:
        self._cfg = cfg
        self._addresses = addresses
        self._upstream = upstream
        self._log_path = log_path
        self._health = (health if health is not None else
                        BridgeHealth(upstream, probe_interval=cfg.probe_interval))
        self.menu = self._build_menu()
        self._icon = (icon if icon is not None else
                      pystray.Icon("socks-http-bridge",
                                   make_icon_image(BridgeState.STOPPED),
                                   "Socks HTTP Bridge", menu=self.menu))
        self._servers: list[socket.socket] = []
        self._clients: set[socket.socket] = set()
        self._failed: list[tuple[str, int]] = []
        self._retry_stop = threading.Event()
        self._last_state = BridgeState.STOPPED
        self._lock = threading.Lock()
        # Lifecycle lock: Stop/Start/Apply mutate _servers/_clients atomically.
        # Separate from _lock: health callbacks take _lock, and holding one
        # shared lock across health.start()'s Probe join would stall them ~3 s.
        self._listener_lock = threading.Lock()
        self._health.subscribe(self._on_state)
        self._refresh_tooltip()

    @property
    def config(self) -> Config:
        return self._cfg

    @property
    def listener_addresses(self) -> list[tuple[str, int]]:
        """The bound Listener addresses (empty while Stopped)."""
        with self._listener_lock:
            return [srv.getsockname() for srv in self._servers]

    def notify(self, message: str, title: str = "Socks HTTP Bridge") -> None:
        """Balloon notification (also the stub seam for feature modules)."""
        self._icon.notify(message, title)

    def _on_state(self, old: BridgeState, new: BridgeState) -> None:
        # Runs on the Probe/worker threads; pystray notify/update_menu are
        # thread-safe on Windows. The event is a hint: re-read state, since
        # delivery order may differ from transition order under races.
        current = self._health.state
        with self._lock:
            previous, self._last_state = self._last_state, current
        if current is BridgeState.DEGRADED and previous is BridgeState.RUNNING:
            failed = ", ".join(f"{h}:{p}" for h, p in list(self._failed))
            self.notify(f"Listener {failed} failed to bind; retrying")
        elif (current is BridgeState.ERROR
              and previous in (BridgeState.RUNNING, BridgeState.DEGRADED)):
            self.notify("Upstream unreachable or Listener failed to bind")
        self._icon.icon = make_icon_image(current)
        self._icon.update_menu()
        self._refresh_tooltip()

    def _refresh_tooltip(self) -> None:
        """Hover tooltip: Bridge State plus a dot per Listener and Upstream.

        Hint only — a snapshot a few ms stale is fine; the next transition
        or bind refreshes it. Takes no _listener_lock: stop() fires
        _on_state synchronously while the caller holds that lock.
        """
        bound: set[tuple[str, int]] = set()
        for srv in list(self._servers):
            try:
                bound.add(srv.getsockname())
            except OSError:
                pass  # closed concurrently by Stop
        state = self._health.state
        if state in (BridgeState.RUNNING, BridgeState.DEGRADED):
            up_dot = DOT_OK
        elif state is BridgeState.ERROR and bound:
            up_dot = DOT_BAD
        else:
            up_dot = DOT_UNKNOWN  # Stopped, or Error with no Probe running
        header = f"Socks HTTP Bridge — {state}"
        up_line = f"{up_dot} Upstream {self._upstream[0]}:{self._upstream[1]}"
        lines = [f"{DOT_OK if a in bound else DOT_BAD} {a[0]}:{a[1]}"
                 for a in self._addresses]
        text = "\n".join([header, *lines, up_line])
        if _tip_len(text) <= TIP_MAX:
            self._icon.title = text
            return
        # Too many Listeners for szTip: keep the head, note the remainder.
        budget = TIP_MAX - _tip_len(header) - _tip_len(up_line) - 12
        kept: list[str] = []
        used = 0
        for line in lines:
            cost = _tip_len(line) + 1
            if used + cost > budget:
                break
            kept.append(line)
            used += cost
        dropped = len(lines) - len(kept)
        text = "\n".join([header, *kept, f"…+{dropped} more", up_line])
        self._icon.title = text.encode("utf-16-le")[:TIP_MAX * 2].decode(
            "utf-16-le", errors="ignore")

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: status_line(self._cfg.listeners, self._cfg.upstream),
                None, enabled=False),
            pystray.MenuItem(
                lambda item: ("Start"
                              if self._health.state is BridgeState.STOPPED
                              else "Stop"),
                self.toggle_bridge),
            pystray.MenuItem("Open log", self.open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit IP Test",
                             lambda icon, item: exit_ip_ui.run_test(self)),
            pystray.MenuItem("Settings",
                             lambda icon, item: settings.open_settings(self)),
            pystray.MenuItem("About…",
                             lambda icon, item: about_ui.show_about(self)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self.exit),
        )

    def start_listeners(self) -> None:
        """Bind every Listener and start the Probe; no-op while serving."""
        with self._listener_lock:
            self._start_listeners_locked()

    def _start_listeners_locked(self) -> None:
        if self._servers:
            return
        self._failed = []
        for host, port in self._addresses:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind((host, port))
                srv.listen(128)
            except OSError as exc:
                # A taken port must not kill the Bridge: the other Listeners
                # keep serving; Bridge State reports Degraded (Error if none).
                srv.close()
                log.error("Listener %s:%s failed to bind: %s", host, port, exc)
                self._failed.append((host, port))
                continue
            self._servers.append(srv)
            log.info("HTTP proxy on %s:%s -> SOCKS5 %s:%s",
                     host, port, *self._upstream)
            threading.Thread(target=self._accept_loop, args=(srv,),
                             daemon=True).start()
        if self._servers:
            self._health.start()
        if self._failed:
            if not self._servers:
                # Unlike the headless entry the tray stays alive: the retry
                # below (or the user) can free the port and Start again.
                log.error("No Listener could take its port")
            # After start(): start() resets Listener failures, a new attempt.
            self._health.listener_failed(partial=bool(self._servers))
            self._retry_stop = threading.Event()
            threading.Thread(target=self._retry_failed_listeners,
                             args=(self._retry_stop,), daemon=True,
                             name="listener-retry").start()

    def _retry_failed_listeners(self, stop: threading.Event) -> None:
        # A Listener address can be absent at logon (the WSL adapter appears
        # later, issue #63): rebind at the Probe cadence until it binds.
        while not stop.wait(self._cfg.probe_interval):
            with self._listener_lock:
                if stop.is_set() or not self._failed:
                    return
                for host, port in list(self._failed):
                    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        srv.bind((host, port))
                        srv.listen(128)
                    except OSError:
                        srv.close()
                        continue
                    self._failed.remove((host, port))
                    self._servers.append(srv)
                    log.info("HTTP proxy on %s:%s -> SOCKS5 %s:%s (rebind)",
                             host, port, *self._upstream)
                    threading.Thread(target=self._accept_loop, args=(srv,),
                                     daemon=True).start()
                    self._refresh_tooltip()  # coverage changed, state maybe not
                if not self._failed:
                    # start() is a no-op while the Probe lives; it starts the
                    # Probe when every Listener had failed at Start.
                    self._health.start()
                    self._health.listener_recovered()
                    return

    def stop_listeners(self) -> None:
        """Close every Listener and stop the Probe (the user Stop)."""
        with self._listener_lock:
            self._stop_listeners_locked()

    def _stop_listeners_locked(self) -> None:
        self._retry_stop.set()
        self._failed.clear()
        for srv in self._servers:
            srv.close()
        self._servers.clear()
        self._health.stop()

    def restart_listeners(self, cfg: Config) -> None:
        """Apply (issue #7): adopt Config and restart Listeners atomically.

        Active Tunnels drop (clients reconnect on their own); the Probe
        follows a changed Upstream/interval. Worker-thread only: the restart
        may join the old Probe for up to ~3 s. Bind failures follow the
        Start pattern: log + listener_failed(), Degraded or Error.
        """
        with self._listener_lock:
            for client in self._clients:
                client.close()
            self._clients.clear()
            self._stop_listeners_locked()
            upstream = parse_address(cfg.upstream, "upstream")
            if (upstream != self._upstream
                    or cfg.probe_interval != self._cfg.probe_interval):
                self._health = BridgeHealth(
                    upstream, probe_interval=cfg.probe_interval)
                self._health.subscribe(self._on_state)
            self._cfg = cfg
            self._addresses = [parse_address(a, "listeners")
                               for a in cfg.listeners]
            self._upstream = upstream
            self._log_path = os.path.expandvars(cfg.log_path)
            self._start_listeners_locked()

    def _accept_loop(self, srv: socket.socket) -> None:
        while True:
            try:
                client, addr = srv.accept()
            except OSError:
                return  # Listener closed by Stop/Exit
            with self._listener_lock:
                self._clients.add(client)
            threading.Thread(target=self._serve_client, args=(client, addr),
                             daemon=True).start()

    def _serve_client(self, client: socket.socket,
                      addr: tuple[str, int]) -> None:
        try:
            handle(client, addr, *self._upstream)
        finally:
            with self._listener_lock:
                self._clients.discard(client)

    def toggle_bridge(self) -> None:
        """Menu Stop/Start; off the UI thread (start may join ~3 s)."""
        threading.Thread(target=self._toggle_worker, daemon=True).start()

    def _toggle_worker(self) -> None:
        with self._listener_lock:
            if self._health.state is BridgeState.STOPPED:
                self._start_listeners_locked()
            else:
                self._stop_listeners_locked()

    def open_log(self) -> None:
        """Open the Config log_path in the system viewer."""
        try:
            os.startfile(self._log_path)
        except OSError as exc:
            self.notify(f"Cannot open log: {exc}")

    def exit(self) -> None:
        """Menu Exit: stop Listeners cleanly, then remove the icon."""
        def worker() -> None:
            self.stop_listeners()
            self._icon.stop()
        threading.Thread(target=worker, daemon=True).start()

    def run(self) -> None:
        """Block in the icon loop; clean up Listeners when it ends."""
        self.start_listeners()
        try:
            self._icon.run()
        finally:
            self.stop_listeners()


MUTEX_NAME = "Local\\SocksHttpBridgeTray"
ERROR_ALREADY_EXISTS = 183  # Win32 error code (winerror module is gone in 3.15)
_mutex_handle: int | None = None  # held for the whole process lifetime


def acquire_single_instance() -> bool:
    """True for the first tray process; a second launch gets False.

    The OS releases the mutex when the holding process exits, so a crashed
    or killed Bridge never locks out the next launch.
    """
    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    # Test seam (issue #28): an env override gives tests a private mutex
    # name; without it production keeps the default MUTEX_NAME.
    handle = kernel32.CreateMutexW(
        None, False, os.environ.get("BRIDGE_MUTEX_NAME", MUTEX_NAME))
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        if handle:
            kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle  # keep the OS handle alive until process exit
    return True


def run(cfg: Config, addresses: list[tuple[str, int]],
        upstream: tuple[str, int], log_path: str) -> None:
    """Tray entry (--tray): single-instance guard, then the icon loop."""
    if not acquire_single_instance():
        log.info("Another Bridge instance is already running; exiting")
        return
    TrayShell(cfg, addresses, upstream, log_path).run()

