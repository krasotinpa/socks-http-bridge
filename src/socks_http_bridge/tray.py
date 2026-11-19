"""Tray Shell: the Bridge lives in the Windows tray (GLOSSARY.md).

One pythonw process, icon coloured by Bridge State, menu with the status
line, Stop/Start, Open log and Exit. Observes Bridge State only through the
Health interface (``state`` + ``subscribe``); no UI code lives in Health or
Tunneling. Feature UIs are separate modules pre-wired into the menu:
exit_ip_ui (issue #6) and settings (issue #7).

Entry: ``pythonw.exe -m socks_http_bridge --tray`` (see __main__.py).
"""

import ctypes
import os
import socket
import threading
from ctypes import wintypes

import pystray  # type: ignore[import-untyped]  # pystray ships no py.typed
from PIL import Image, ImageDraw

from socks_http_bridge import exit_ip_ui, settings
from socks_http_bridge.config import Config, parse_address
from socks_http_bridge.health import BridgeHealth, BridgeState
from socks_http_bridge.tunneling import handle, log

STATE_COLORS: dict[BridgeState, tuple[int, int, int]] = {
    BridgeState.RUNNING: (0, 160, 0),    # green
    BridgeState.STOPPED: (128, 128, 128),  # grey
    BridgeState.ERROR: (200, 0, 0),      # red
}


def make_icon_image(state: BridgeState) -> Image.Image:
    """A coloured circle on transparent background; no external assets."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56),
                                  fill=(*STATE_COLORS[state], 255))
    return image


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
        self._last_state = BridgeState.STOPPED
        self._lock = threading.Lock()
        # Lifecycle lock: Stop/Start/Apply mutate _servers/_clients atomically.
        # Separate from _lock: health callbacks take _lock, and holding one
        # shared lock across health.start()'s Probe join would stall them ~3 s.
        self._listener_lock = threading.Lock()
        self._health.subscribe(self._on_state)

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
        if previous is BridgeState.RUNNING and current is BridgeState.ERROR:
            self.notify("Upstream unreachable or Listener failed to bind")
        self._icon.icon = make_icon_image(current)
        self._icon.update_menu()

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
        bind_failed = False
        for host, port in self._addresses:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind((host, port))
                srv.listen(128)
            except OSError as exc:
                # A taken port must not kill the Bridge: the other Listeners
                # keep serving, and Bridge State reports Error.
                srv.close()
                log.error("Listener %s:%s failed to bind: %s", host, port, exc)
                bind_failed = True
                continue
            self._servers.append(srv)
            log.info("HTTP proxy on %s:%s -> SOCKS5 %s:%s",
                     host, port, *self._upstream)
            threading.Thread(target=self._accept_loop, args=(srv,),
                             daemon=True).start()
        if not self._servers:
            # Unlike the headless entry the tray stays alive: the user can
            # free the port and Start again (Settings/Apply, issue #7).
            log.error("No Listener could take its port")
            self._health.listener_failed()
            return
        self._health.start()
        if bind_failed:
            # After start(): start() resets Listener failures, a new attempt.
            self._health.listener_failed()

    def stop_listeners(self) -> None:
        """Close every Listener and stop the Probe (the user Stop)."""
        with self._listener_lock:
            self._stop_listeners_locked()

    def _stop_listeners_locked(self) -> None:
        for srv in self._servers:
            srv.close()
        self._servers.clear()
        self._health.stop()

    def restart_listeners(self, cfg: Config) -> None:
        """Apply (issue #7): adopt Config and restart Listeners atomically.

        Active Tunnels drop (clients reconnect on their own); the Probe
        follows a changed Upstream/interval. Worker-thread only: the restart
        may join the old Probe for up to ~3 s. Bind failures follow the
        Start pattern: log + listener_failed(), Bridge State Error.
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

