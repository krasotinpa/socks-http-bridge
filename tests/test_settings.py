"""Settings tests: form validation, LAN-exposure warning, Apply orchestration,
Listener lifecycle lock and the tkinter form (withdrawn root, no mainloop).

The live Bridge on 127.0.0.1:8118 and the AdGuard Upstream on 127.0.0.1:9090
are never touched: every socket binds port 0, Config writes go to tmp_path.
"""

import json
import socket
import threading
import time
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fake_upstream import FakeUpstream
from loopback import LoopbackServer
from targets import EchoTarget
from test_tray import FakeIcon

from socks_http_bridge import settings, tray
from socks_http_bridge.config import Config, ConfigError
from socks_http_bridge.health import BridgeHealth, BridgeState


@pytest.fixture(autouse=True)
def _isolated_config_path(tmp_path: Path,
                          monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never write the real per-user config.json from a test."""
    path = tmp_path / "config.json"
    monkeypatch.setattr("socks_http_bridge.config.default_config_path",
                        lambda: path)
    return path


@pytest.fixture(autouse=True)
def _fake_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #8 wired the real shortcut core into the form; these #7 pins
    must not touch the real Startup folder. The fake stays in sync with the
    Config default (autostart=True) so the prefill pin keeps its meaning;
    dedicated wiring coverage lives in test_autostart_ui.py."""
    monkeypatch.setattr(settings.autostart, "shortcut_exists",
                        lambda _dir: True)
    monkeypatch.setattr(settings.autostart, "create_shortcut",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(settings.autostart, "remove_shortcut",
                        lambda _dir: None)


def _form(**overrides: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "listeners": ["127.0.0.1:8118"],
        "upstream": "127.0.0.1:9090",
        "autostart": True,
        "probe_interval": "10",
        "log_path": "bridge.log",
        "test_url": "https://ipinfo.io/json",
    }
    form.update(overrides)
    return form


# --- form validation (pure logic, config.py rules at the UI seam) ---


def test_valid_form_builds_config() -> None:
    cfg = settings.config_from_form(**_form())
    assert cfg == Config(listeners=["127.0.0.1:8118"], upstream="127.0.0.1:9090",
                         autostart=True, probe_interval=10,
                         log_path="bridge.log", test_url="https://ipinfo.io/json")


def test_bad_port_rejected_with_message() -> None:
    with pytest.raises(ConfigError, match="outside 1..65535"):
        settings.config_from_form(**_form(listeners=["127.0.0.1:99999"]))
    with pytest.raises(ConfigError, match="not an integer"):
        settings.config_from_form(**_form(listeners=["127.0.0.1:http"]))
    with pytest.raises(ConfigError, match="not host:port"):
        settings.config_from_form(**_form(upstream="127.0.0.1"))


def test_duplicate_listener_rejected() -> None:
    with pytest.raises(ConfigError, match="duplicate Listener"):
        settings.config_from_form(
            **_form(listeners=["127.0.0.1:8118", "127.0.0.1:08118"]))


def test_empty_listeners_rejected() -> None:
    with pytest.raises(ConfigError, match="non-empty"):
        settings.config_from_form(**_form(listeners=[]))


def test_interval_out_of_range_rejected() -> None:
    for text in ("1", "301", "abc", ""):
        with pytest.raises(ConfigError, match="probe_interval"):
            settings.config_from_form(**_form(probe_interval=text))


def test_interval_boundaries_accepted() -> None:
    assert settings.config_from_form(**_form(probe_interval="2")).probe_interval == 2
    assert settings.config_from_form(**_form(probe_interval="300")).probe_interval == 300


# --- LAN-exposure warning (pure logic) ---


def test_non_loopback_listener_warns() -> None:
    warning = settings.lan_warning(["0.0.0.0:8118"])
    assert warning is not None
    assert "0.0.0.0:8118" in warning and "LAN" in warning
    assert settings.lan_warning(["127.0.0.1:8118", "192.168.1.5:8119"]) is not None


def test_loopback_listeners_do_not_warn() -> None:
    assert settings.lan_warning(
        ["127.0.0.1:8118", "localhost:8119", "127.0.0.2:8120"]) is None


# --- Apply orchestration and Listener lifecycle (tray seams) ---


class FakeShell:
    """Records Apply calls; proves Config was saved before the restart."""

    def __init__(self, cfg: Config, config_path: Path) -> None:
        self.config = cfg
        self._config_path = config_path
        self.restarted: list[Config] = []
        self.saved_at_restart: list[dict[str, object]] = []
        self.restart_event = threading.Event()
        self.notifications: list[str] = []

    def notify(self, message: str, title: str = "Socks HTTP Bridge") -> None:
        self.notifications.append(message)

    def restart_listeners(self, cfg: Config) -> None:
        self.saved_at_restart.append(
            json.loads(self._config_path.read_text(encoding="utf-8")))
        self.restarted.append(cfg)
        self.restart_event.set()


def _listener_socket(port: int = 0) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(8)
    return s


class _ProbeUpstream(LoopbackServer):
    """Accepts and closes Probes so the listen backlog never fills up."""

    def _handle(self, conn: socket.socket) -> None:
        conn.close()


def _free_port() -> int:
    s = _listener_socket()
    port: int = s.getsockname()[1]
    s.close()
    return port


def _serves(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass


def _refused(port: int) -> None:
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.5)


def _wait_state(health: BridgeHealth, state: BridgeState,
                timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health.state is state:
            return
        time.sleep(0.02)
    raise AssertionError(f"Bridge State stuck at {health.state}, wanted {state}")


def _wait_serves(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _serves(port)
            return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"Listener on {port} never served")


def test_apply_saves_new_config_then_restarts(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    shell = FakeShell(Config(), path)
    cfg = Config(listeners=["127.0.0.1:18118"], upstream="127.0.0.1:19090",
                 autostart=False, probe_interval=7,
                 log_path=str(tmp_path / "bridge.log"),
                 test_url="https://api.ipify.org")
    settings.apply_config(shell, cfg, path)
    assert shell.restarted == [cfg]
    # The restart saw the NEW Config on disk: save happened first.
    assert shell.saved_at_restart == [json.loads(path.read_text(encoding="utf-8"))]
    assert json.loads(path.read_text(encoding="utf-8"))["listeners"] == [
        "127.0.0.1:18118"]


def test_restart_closes_old_port_and_serves_new(tmp_path: Path) -> None:
    with _ProbeUpstream() as upstream:
        old_port, new_port = _free_port(), _free_port()
        log_path = str(tmp_path / "bridge.log")
        up_text = f"{upstream.address[0]}:{upstream.address[1]}"
        cfg_old = Config(listeners=[f"127.0.0.1:{old_port}"], upstream=up_text,
                         log_path=log_path)
        health = BridgeHealth(upstream.address, probe_interval=0.05)
        shell = tray.TrayShell(cfg_old, [("127.0.0.1", old_port)],
                               upstream.address, log_path,
                               icon=FakeIcon(), health=health)
        try:
            shell.start_listeners()
            _wait_state(health, BridgeState.RUNNING)
            _serves(old_port)
            cfg_new = Config(listeners=[f"127.0.0.1:{new_port}"],
                             upstream=up_text, log_path=log_path)
            shell.restart_listeners(cfg_new)
            _wait_state(health, BridgeState.RUNNING)
            _refused(old_port)
            _serves(new_port)
            assert shell.config is cfg_new
            assert shell.listener_addresses == [("127.0.0.1", new_port)]
        finally:
            shell.stop_listeners()


def test_restart_probe_follows_new_upstream(tmp_path: Path) -> None:
    with _ProbeUpstream() as upstream_old, _ProbeUpstream() as upstream_new:
        port = _free_port()
        log_path = str(tmp_path / "bridge.log")
        icon = FakeIcon()
        cfg_old = Config(
            listeners=[f"127.0.0.1:{port}"],
            upstream=f"127.0.0.1:{upstream_old.address[1]}",
            probe_interval=2, log_path=log_path)
        shell = tray.TrayShell(cfg_old, [("127.0.0.1", port)],
                               upstream_old.address, log_path,
                               icon=icon, health=BridgeHealth(
                                   upstream_old.address, probe_interval=0.05))
        try:
            shell.start_listeners()
            _wait_serves(port)
            cfg_new = Config(
                listeners=[f"127.0.0.1:{port}"],
                upstream=f"127.0.0.1:{upstream_new.address[1]}",
                probe_interval=2, log_path=log_path)
            shell.restart_listeners(cfg_new)
            _wait_serves(port)
            # The balloon fires only on Running->Error (issue #5 dedup in
            # tray._on_state). The recreated Health starts Stopped and its
            # first Probe is merely scheduled when restart returns, so wait
            # for Running before killing the new Upstream; otherwise the
            # transition is Stopped->Error and no balloon is due by design.
            health = shell._health
            deadline = time.monotonic() + 5
            while (time.monotonic() < deadline
                   and health.state is not BridgeState.RUNNING):
                time.sleep(0.02)
            assert health.state is BridgeState.RUNNING, \
                "new Probe never reached Running"
            # Killing only the NEW Upstream must flip Bridge State to Error
            # (balloon seam): the Probe no longer watches the old one.
            upstream_new.stop()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not icon.notifications:
                time.sleep(0.02)
            assert icon.notifications, "Probe did not follow the new Upstream"
            assert "Upstream unreachable" in icon.notifications[-1][0]
        finally:
            shell.stop_listeners()


def test_restart_bind_failure_reports_error_and_keeps_others(
        tmp_path: Path) -> None:
    with _ProbeUpstream() as upstream:
        # Exclusive taker (no SO_REUSEADDR): on Windows REUSEADDR sockets may
        # share a port, so the taker must be exclusive (as in test_tray).
        taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        taken.bind(("127.0.0.1", 0))
        taken_port = taken.getsockname()[1]
        free_port = _free_port()
        log_path = str(tmp_path / "bridge.log")
        up_text = f"127.0.0.1:{upstream.address[1]}"
        health = BridgeHealth(upstream.address, probe_interval=0.05)
        shell = tray.TrayShell(Config(listeners=[f"127.0.0.1:{free_port}"],
                                      upstream=up_text, log_path=log_path),
                               [("127.0.0.1", free_port)], upstream.address,
                               log_path, icon=FakeIcon(), health=health)
        try:
            shell.start_listeners()
            _wait_state(health, BridgeState.RUNNING)
            cfg = Config(listeners=[f"127.0.0.1:{taken_port}",
                                    f"127.0.0.1:{free_port}"],
                         upstream=up_text, log_path=log_path)
            shell.restart_listeners(cfg)
            _wait_state(health, BridgeState.ERROR)
            _serves(free_port)  # the other Listener keeps serving
            _refused(taken_port)
            taken.close()
            shell.restart_listeners(cfg)  # a new Apply resets Listener failures
            _wait_state(health, BridgeState.RUNNING)
            _serves(taken_port)
        finally:
            shell.stop_listeners()
            taken.close()


class CountingSocket(socket.socket):
    """Counts live binds per port: a double bind leaks a live socket."""

    live_binds: ClassVar[dict[int, int]] = {}

    def bind(self, address: tuple[str, int]) -> None:
        super().bind(address)
        self._counted_port: int | None = address[1]
        CountingSocket.live_binds[address[1]] = (
            CountingSocket.live_binds.get(address[1], 0) + 1)

    def close(self) -> None:
        port = getattr(self, "_counted_port", None)
        if port is not None:
            CountingSocket.live_binds[port] -= 1
            self._counted_port = None
        super().close()


def test_concurrent_stop_start_apply_never_double_binds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    CountingSocket.live_binds.clear()
    monkeypatch.setattr(socket, "socket", CountingSocket)
    with _ProbeUpstream() as upstream:
        port = _free_port()
        log_path = str(tmp_path / "bridge.log")
        up_text = f"127.0.0.1:{upstream.address[1]}"
        cfg = Config(listeners=[f"127.0.0.1:{port}"], upstream=up_text,
                     log_path=log_path)
        shell = tray.TrayShell(cfg, [("127.0.0.1", port)], upstream.address,
                               log_path, icon=FakeIcon(),
                               health=BridgeHealth(upstream.address,
                                                   probe_interval=0.05))
        errors: list[BaseException] = []

        def storm() -> None:
            try:
                for _ in range(25):
                    shell.stop_listeners()
                    shell.start_listeners()
            except BaseException as exc:  # noqa: BLE001 - record any race failure
                errors.append(exc)

        def apply_storm() -> None:
            try:
                for _ in range(8):
                    shell.restart_listeners(cfg)
            except BaseException as exc:  # noqa: BLE001 - record any race failure
                errors.append(exc)

        try:
            threads = [threading.Thread(target=storm) for _ in range(3)]
            threads.append(threading.Thread(target=apply_storm))
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            assert not errors, f"lifecycle race raised: {errors}"
            shell.start_listeners()  # normalise: serving after the storm
            assert CountingSocket.live_binds.get(port) == 1, "double bind leaked"
            assert shell.listener_addresses == [("127.0.0.1", port)]
            _serves(port)
        finally:
            shell.stop_listeners()


def test_apply_drops_active_tunnel_without_hanging(tmp_path: Path) -> None:
    with EchoTarget() as target, FakeUpstream(target.address) as upstream:
        old_port, new_port = _free_port(), _free_port()
        log_path = str(tmp_path / "bridge.log")
        up_text = f"127.0.0.1:{upstream.address[1]}"
        cfg_old = Config(listeners=[f"127.0.0.1:{old_port}"], upstream=up_text,
                         log_path=log_path)
        shell = tray.TrayShell(cfg_old, [("127.0.0.1", old_port)],
                               upstream.address, log_path, icon=FakeIcon(),
                               health=BridgeHealth(upstream.address,
                                                   probe_interval=0.05))
        try:
            shell.start_listeners()
            client = socket.create_connection(("127.0.0.1", old_port),
                                              timeout=5)
            client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\n"
                           b"Host: example.test\r\n\r\n")
            head = b""
            while b"\r\n\r\n" not in head:
                head += client.recv(4096)
            assert head.startswith(b"HTTP/1.1 200")
            client.sendall(b"ping")
            assert client.recv(4096) == b"ping"  # Tunnel is live
            cfg_new = Config(listeners=[f"127.0.0.1:{new_port}"],
                             upstream=up_text, log_path=log_path)
            started = time.monotonic()
            settings.apply_config(shell, cfg_new, tmp_path / "config.json")
            assert time.monotonic() - started < 5  # no hang on the live Tunnel
            client.settimeout(5)
            try:
                data = client.recv(4096)
            except OSError:
                data = b""  # reset is a drop too
            assert data == b"", "active Tunnel survived Apply"
            _refused(old_port)
            _serves(new_port)
        finally:
            client.close()
            shell.stop_listeners()


# --- tkinter form (withdrawn root, no mainloop) ---


@pytest.fixture()
def root() -> Iterator[tk.Tk]:
    window = tk.Tk()
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass  # already destroyed by OK/Cancel


def _ui_shell(tmp_path: Path) -> FakeShell:
    return FakeShell(Config(), tmp_path / "config.json")


def _alive(window: tk.Tk) -> bool:
    try:
        return bool(window.winfo_exists())
    except tk.TclError:
        return False


def test_form_prefills_from_config(root: tk.Tk, tmp_path: Path) -> None:
    shell = _ui_shell(tmp_path)
    form = settings.SettingsForm(shell, root)
    assert list(form.listeners.get(0, tk.END)) == shell.config.listeners
    assert form.upstream.get() == shell.config.upstream
    assert form.autostart.get() == shell.config.autostart
    assert form.probe_interval.get() == str(shell.config.probe_interval)
    assert form.log_path.get() == shell.config.log_path
    assert form.test_url.get() == shell.config.test_url
    assert form.warning.cget("text") == ""
    assert form.error.cget("text") == ""


def test_add_remove_listener_updates_lan_warning(root: tk.Tk,
                                                 tmp_path: Path) -> None:
    form = settings.SettingsForm(_ui_shell(tmp_path), root)
    form.listener_entry.insert(0, "0.0.0.0:8118")
    form.add_listener()
    assert list(form.listeners.get(0, tk.END)) == ["127.0.0.1:8118",
                                                   "0.0.0.0:8118"]
    assert "LAN" in form.warning.cget("text")
    assert "0.0.0.0:8118" in form.warning.cget("text")
    form.listeners.selection_set(1)
    form.remove_listener()
    assert list(form.listeners.get(0, tk.END)) == ["127.0.0.1:8118"]
    assert form.warning.cget("text") == ""


def test_ok_invalid_interval_inline_error_stays_open(root: tk.Tk,
                                                     tmp_path: Path) -> None:
    shell = _ui_shell(tmp_path)
    form = settings.SettingsForm(shell, root)
    form.probe_interval.delete(0, tk.END)
    form.probe_interval.insert(0, "1")
    form.on_ok()
    assert "probe_interval" in form.error.cget("text")
    assert _alive(root), "window closed on invalid input"
    assert shell.restarted == []
    assert not (tmp_path / "config.json").exists()


def test_ok_bad_port_inline_error(root: tk.Tk, tmp_path: Path) -> None:
    shell = _ui_shell(tmp_path)
    form = settings.SettingsForm(shell, root)
    form.listener_entry.insert(0, "127.0.0.1:99999")
    form.add_listener()
    form.on_ok()
    assert "outside 1..65535" in form.error.cget("text")
    assert _alive(root)
    assert shell.restarted == []


def test_ok_duplicate_listener_inline_error(root: tk.Tk,
                                            tmp_path: Path) -> None:
    shell = _ui_shell(tmp_path)
    form = settings.SettingsForm(shell, root)
    form.listener_entry.insert(0, "127.0.0.1:08118")
    form.add_listener()
    form.on_ok()
    assert "duplicate" in form.error.cget("text")
    assert _alive(root)
    assert shell.restarted == []


def test_ok_applies_saves_and_closes(root: tk.Tk, tmp_path: Path) -> None:
    shell = _ui_shell(tmp_path)
    form = settings.SettingsForm(shell, root)
    form.upstream.delete(0, tk.END)
    form.upstream.insert(0, "127.0.0.1:19090")
    form.autostart.set(False)
    form.on_ok()
    assert shell.restart_event.wait(5), "Apply worker never restarted"
    assert not _alive(root), "OK must close the window"
    cfg = shell.restarted[0]
    assert cfg.upstream == "127.0.0.1:19090"
    assert cfg.autostart is False
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["upstream"] == "127.0.0.1:19090"
    assert saved["autostart"] is False
    assert shell.saved_at_restart == [saved]  # save happened before restart


def test_cancel_discards_everything(root: tk.Tk, tmp_path: Path) -> None:
    shell = _ui_shell(tmp_path)
    form = settings.SettingsForm(shell, root)
    form.upstream.delete(0, tk.END)
    form.upstream.insert(0, "127.0.0.1:19090")
    form.listener_entry.insert(0, "0.0.0.0:8118")
    form.add_listener()
    form.on_cancel()
    assert not _alive(root)
    assert shell.restarted == []
    assert not (tmp_path / "config.json").exists()


def test_open_settings_runs_window_on_its_own_thread(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    done = threading.Event()

    class FakeRoot:
        def mainloop(self) -> None:
            seen["mainloop_thread"] = threading.current_thread()
            done.set()

        def destroy(self) -> None:
            pass

    def fake_form(shell: Any, window: Any) -> None:
        seen["shell"] = shell
        seen["root"] = window

    monkeypatch.setattr(settings.tk, "Tk", FakeRoot)
    monkeypatch.setattr(settings, "SettingsForm", fake_form)
    shell = _ui_shell(tmp_path)
    settings.open_settings(shell)
    assert done.wait(5), "window thread never reached mainloop"
    assert seen["mainloop_thread"] is not threading.current_thread()
    assert seen["shell"] is shell
    assert isinstance(seen["root"], FakeRoot)