"""Tray Shell tests: pure state mapping, balloon dedup at the subscribe
handler seam, Stop/Start wiring on real sockets, menu stubs, single-instance
mutex. No real icon loop runs here; the live Bridge on 127.0.0.1:8118 and the
AdGuard Upstream on 127.0.0.1:9090 are never touched (all sockets bind 0).
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path

import pytest
from test_cli_config import _wait_for_listener

from socks_http_bridge import tray
from socks_http_bridge.config import Config
from socks_http_bridge.health import BridgeHealth, BridgeState


class FakeIcon:
    """Records what the shell does to the icon; no real tray loop runs."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str | None]] = []
        self.icon: object = None
        self.menu_updates = 0
        self.stopped = threading.Event()

    def notify(self, message: str, title: str | None = None) -> None:
        self.notifications.append((message, title))

    def update_menu(self) -> None:
        self.menu_updates += 1

    def stop(self) -> None:
        self.stopped.set()


class FakeHealth:
    """State holder with the Health subscribe seam, no Probe thread."""

    def __init__(self, state: BridgeState = BridgeState.STOPPED) -> None:
        self.state = state
        self.callbacks: list[Callable[[BridgeState, BridgeState], None]] = []

    def subscribe(self, callback: Callable[[BridgeState, BridgeState], None]) -> None:
        self.callbacks.append(callback)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.state = BridgeState.STOPPED

    def listener_failed(self) -> None:
        self.state = BridgeState.ERROR


def _wait_state(health: BridgeHealth, state: BridgeState,
                timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health.state is state:
            return
        time.sleep(0.02)
    raise AssertionError(f"Bridge State stuck at {health.state}, wanted {state}")


def _listener_socket(port: int = 0) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(8)
    return s



def test_icon_image_colour_per_state() -> None:
    # Independent truth: green Running, grey Stopped, red Error (issue #5).
    expected = {
        BridgeState.RUNNING: (0, 160, 0),
        BridgeState.STOPPED: (128, 128, 128),
        BridgeState.ERROR: (200, 0, 0),
    }
    for state, rgb in expected.items():
        image = tray.make_icon_image(state)
        assert image.getpixel((32, 32)) == (*rgb, 255)


def test_status_line_first_listener_plus_count() -> None:
    upstream = "127.0.0.1:9090"
    assert tray.status_line(["127.0.0.1:8118"], upstream) == (
        "127.0.0.1:8118 -> 127.0.0.1:9090")
    assert tray.status_line(
        ["127.0.0.1:8118", "127.0.0.1:8119", "127.0.0.1:8120"], upstream) == (
        "127.0.0.1:8118 -> 127.0.0.1:9090 (+2)")


def test_one_balloon_on_running_to_error_and_silent_recovery(
        tmp_path: Path) -> None:
    log_path = str(tmp_path / "bridge.log")
    upstream = _listener_socket()
    address = upstream.getsockname()
    health = BridgeHealth(address, probe_interval=0.05)
    icon = FakeIcon()
    tray.TrayShell(Config(log_path=log_path), [], address, log_path,
                   icon=icon, health=health)
    try:
        health.start()
        _wait_state(health, BridgeState.RUNNING)
        upstream.close()
        _wait_state(health, BridgeState.ERROR)
        assert len(icon.notifications) == 1
        time.sleep(0.2)  # repeated Probes while Error must not spam
        assert len(icon.notifications) == 1
        recovered = _listener_socket(address[1])
        try:
            _wait_state(health, BridgeState.RUNNING)
            assert len(icon.notifications) == 1  # recovery stays silent
        finally:
            recovered.close()
    finally:
        health.stop()


def test_stale_transition_event_rereads_state(tmp_path: Path) -> None:
    log_path = str(tmp_path / "bridge.log")
    health = FakeHealth(BridgeState.RUNNING)
    icon = FakeIcon()
    tray.TrayShell(Config(log_path=log_path), [], ("127.0.0.1", 9), log_path,
                   icon=icon, health=health)
    on_state = health.callbacks[0]
    # A delayed Running->Error hint delivered after recovery: state is truth.
    on_state(BridgeState.RUNNING, BridgeState.ERROR)
    assert icon.notifications == []
    health.state = BridgeState.ERROR
    on_state(BridgeState.RUNNING, BridgeState.ERROR)
    assert len(icon.notifications) == 1


def _shell_on_port(tmp_path: Path, port: int,
                   upstream: tuple[str, int]) -> tuple[tray.TrayShell,
                                                       BridgeHealth, FakeIcon]:
    log_path = str(tmp_path / "bridge.log")
    cfg = Config(listeners=[f"127.0.0.1:{port}"],
                 upstream=f"{upstream[0]}:{upstream[1]}", log_path=log_path)
    health = BridgeHealth(upstream, probe_interval=0.05)
    icon = FakeIcon()
    shell = tray.TrayShell(cfg, [("127.0.0.1", port)], upstream, log_path,
                           icon=icon, health=health)
    return shell, health, icon


def _serves(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass


def _refused(port: int) -> None:
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.5)


def test_toggle_stops_and_starts_listeners(tmp_path: Path) -> None:
    upstream = _listener_socket()
    reserved = _listener_socket()
    port = reserved.getsockname()[1]
    reserved.close()
    shell, health, _icon = _shell_on_port(tmp_path, port, upstream.getsockname())
    try:
        shell.start_listeners()
        _wait_state(health, BridgeState.RUNNING)
        assert shell.listener_addresses == [("127.0.0.1", port)]
        _serves(port)
        shell.toggle_bridge()  # the menu Stop action
        _wait_state(health, BridgeState.STOPPED)
        _refused(port)
        shell.toggle_bridge()  # the menu Start action
        _wait_state(health, BridgeState.RUNNING)
        _serves(port)
    finally:
        shell.stop_listeners()
        upstream.close()


def test_bind_failure_keeps_error_until_restart(tmp_path: Path) -> None:
    upstream = _listener_socket()
    # Kept bound WITHOUT SO_REUSEADDR: on Windows two REUSEADDR sockets may
    # share a port, so the taker must be exclusive (as in test_cli_health).
    taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    taken.bind(("127.0.0.1", 0))
    taken_port = taken.getsockname()[1]
    reserved = _listener_socket()
    free_port = reserved.getsockname()[1]
    reserved.close()
    log_path = str(tmp_path / "bridge.log")
    cfg = Config(listeners=[f"127.0.0.1:{taken_port}", f"127.0.0.1:{free_port}"],
                 upstream=f"{upstream.getsockname()[0]}:{upstream.getsockname()[1]}",
                 log_path=log_path)
    health = BridgeHealth(upstream.getsockname(), probe_interval=0.05)
    shell = tray.TrayShell(cfg, [("127.0.0.1", taken_port),
                                 ("127.0.0.1", free_port)],
                           upstream.getsockname(), log_path,
                           icon=FakeIcon(), health=health)
    try:
        shell.start_listeners()
        _wait_state(health, BridgeState.ERROR)
        _serves(free_port)  # the other Listener keeps serving
        assert shell.listener_addresses == [("127.0.0.1", free_port)]
        shell.stop_listeners()
        taken.close()
        shell.start_listeners()  # a new start resets Listener failures
        _wait_state(health, BridgeState.RUNNING)
        _serves(taken_port)
        _serves(free_port)
    finally:
        shell.stop_listeners()
        taken.close()
        upstream.close()


def test_menu_wires_status_stubs_and_actions(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = str(tmp_path / "bridge.log")
    cfg = Config(listeners=["127.0.0.1:8118", "127.0.0.1:8119"],
                 upstream="127.0.0.1:9090", log_path=log_path)
    health = FakeHealth()
    icon = FakeIcon()
    shell = tray.TrayShell(cfg, [], ("127.0.0.1", 9090), log_path,
                           icon=icon, health=health)
    items = {item.text: item for item in shell.menu.items}
    status = items["127.0.0.1:8118 -> 127.0.0.1:9090 (+1)"]
    assert status.enabled is False
    assert items["Start"].enabled is True  # Stopped -> the action is Start
    health.state = BridgeState.RUNNING
    assert "Stop" in {item.text for item in shell.menu.items}
    for label in ("Open log", "Exit", "Exit IP Test", "Settings"):
        assert label in items
    # Exit IP Test (#6): no Listener is bound, so the daemon worker balloons.
    items["Exit IP Test"](icon)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not icon.notifications:
        time.sleep(0.02)
    assert icon.notifications, "Exit IP Test worker never notified"
    assert "no Listener is bound" in icon.notifications[-1][0]
    # Settings (#7): the menu opens the real window on its own thread.
    # Build it on a withdrawn root with mainloop stubbed: no visible window.
    # Tk is thread-affine, so the checks run inside the stubbed mainloop,
    # which executes on the window thread after SettingsForm is built.
    real_tk = tk.Tk  # capture before patching the module attribute
    seen: dict[str, object] = {}
    done = threading.Event()

    def withdrawn_tk() -> tk.Tk:
        root = real_tk()
        root.withdraw()
        return root

    def fake_mainloop(self: tk.Tk) -> None:
        seen["title"] = self.title()
        seen["thread"] = threading.current_thread()
        self.destroy()
        done.set()

    monkeypatch.setattr(tray.settings.tk, "Tk", withdrawn_tk)
    monkeypatch.setattr(real_tk, "mainloop", fake_mainloop)
    items["Settings"](icon)
    assert done.wait(3), "Settings window never reached mainloop"
    assert seen["title"] == "Socks HTTP Bridge — Settings"
    assert seen["thread"] is not threading.current_thread()
    # About (#35): the menu dispatches the popup on its own worker thread.
    about_done = threading.Event()
    about_threads: list[threading.Thread] = []

    def about_popup(shell: object) -> None:
        about_threads.append(threading.current_thread())
        about_done.set()

    monkeypatch.setattr(tray.about_ui, "show_popup", about_popup)
    items["About…"](icon)
    assert about_done.wait(3), "About popup never opened"
    assert about_threads[0] is not threading.current_thread()


def test_open_log_opens_config_log_path(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = str(tmp_path / "bridge.log")
    icon = FakeIcon()
    shell = tray.TrayShell(Config(log_path=log_path), [], ("127.0.0.1", 9),
                           log_path, icon=icon, health=FakeHealth())
    opened: list[str] = []
    monkeypatch.setattr(tray.os, "startfile", opened.append, raising=False)
    shell.open_log()
    assert opened == [log_path]

    def no_viewer(path: str) -> None:
        raise OSError("no viewer")

    monkeypatch.setattr(tray.os, "startfile", no_viewer, raising=False)
    shell.open_log()  # a missing viewer is reported, never raised
    assert "no viewer" in icon.notifications[-1][0]


def test_exit_stops_listeners_and_removes_icon(tmp_path: Path) -> None:
    upstream = _listener_socket()
    reserved = _listener_socket()
    port = reserved.getsockname()[1]
    reserved.close()
    shell, health, icon = _shell_on_port(tmp_path, port, upstream.getsockname())
    try:
        shell.start_listeners()
        _wait_state(health, BridgeState.RUNNING)
        shell.exit()
        assert icon.stopped.wait(5)
        _wait_state(health, BridgeState.STOPPED)
        _refused(port)
    finally:
        shell.stop_listeners()
        upstream.close()


def test_second_instance_exits_silently_and_logs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The real --tray entry with only the icon loop replaced by a sleep, so
    # no tray icon ever appears; mutex guard and CLI wiring are the real ones.
    # A private mutex name, inherited by both children, keeps the test
    # hermetic while the live tray holds the default one (issue #28).
    monkeypatch.setenv("BRIDGE_MUTEX_NAME",
                       f"Local\\SocksHttpBridgeTray-test-{os.getpid()}")
    driver = (
        "import sys, time; "
        "sys.argv = ['socks-http-bridge', '--tray', '--config', sys.argv[1]]; "
        "from socks_http_bridge import tray; "
        "tray.pystray.Icon.run = lambda self: time.sleep(60); "
        "from socks_http_bridge.__main__ import main; main()"
    )
    upstream = _listener_socket()
    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved.bind(("127.0.0.1", 0))
    port = reserved.getsockname()[1]
    reserved.close()
    log_path = tmp_path / "bridge.log"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "listeners": [f"127.0.0.1:{port}"],
        "upstream": f"{upstream.getsockname()[0]}:{upstream.getsockname()[1]}",
        "log_path": str(log_path),
    }), encoding="utf-8")
    cmd = [sys.executable, "-c", driver, str(config_path)]
    first = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        _wait_for_listener(first, port)
        second = subprocess.run(cmd, check=False, capture_output=True,
                                text=True, timeout=30)
        assert second.returncode == 0
        assert second.stdout == "" and second.stderr == ""  # silent
        assert ("Another Bridge instance is already running; exiting"
                in log_path.read_text(encoding="utf-8"))
        assert first.poll() is None  # the first instance is unaffected
        _serves(port)
    finally:
        first.terminate()
        first.wait(timeout=10)
        upstream.close()


