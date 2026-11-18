"""Autostart wiring in Settings (issue #8): the checkbox reflects the real
Startup shortcut, Apply creates/removes it through the #15 core and keeps
``config.autostart`` in sync. Headless: withdrawn Tk root, no mainloop; the
real Startup folder is never touched (fakes record every core call). One
integration test uses a REAL .lnk in a tmp_path Startup dir.
"""

import json
import subprocess
import tkinter as tk
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_autostart import PYTHONW, read_lnk
from test_settings import FakeShell

from socks_http_bridge import autostart, settings
from socks_http_bridge.config import Config

TRAY_ARGS = "-m socks_http_bridge --tray"


@pytest.fixture(autouse=True)
def _isolated_config_path(tmp_path: Path,
                          monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never write the real per-user config.json from a test."""
    path = tmp_path / "config.json"
    monkeypatch.setattr("socks_http_bridge.config.default_config_path",
                        lambda: path)
    return path


class FakeAutostart:
    """Records exists/create/remove calls; optionally fails create/remove."""

    def __init__(self, present: bool) -> None:
        self.present = present
        self.exists_calls: list[Path] = []
        self.create_calls: list[tuple[Path, Path, dict[str, Any]]] = []
        self.remove_calls: list[Path] = []
        self.create_error: Exception | None = None
        self.remove_error: Exception | None = None

    def exists(self, startup_dir: Path) -> bool:
        self.exists_calls.append(startup_dir)
        return self.present

    def create(self, startup_dir: Path, pythonw: Path,
               **kwargs: Any) -> Path:
        self.create_calls.append((startup_dir, pythonw, kwargs))
        if self.create_error is not None:
            raise self.create_error
        self.present = True
        return startup_dir / autostart.SHORTCUT_NAME

    def remove(self, startup_dir: Path) -> None:
        self.remove_calls.append(startup_dir)
        if self.remove_error is not None:
            raise self.remove_error
        self.present = False


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeAutostart:
    """Default: no shortcut; tests set ``fake.present`` before opening."""
    fake = FakeAutostart(present=False)
    monkeypatch.setattr(autostart, "shortcut_exists", fake.exists)
    monkeypatch.setattr(autostart, "create_shortcut", fake.create)
    monkeypatch.setattr(autostart, "remove_shortcut", fake.remove)
    return fake


@pytest.fixture()
def root() -> Iterator[tk.Tk]:
    window = tk.Tk()
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass  # already destroyed by OK/Cancel


def _alive(window: tk.Tk) -> bool:
    try:
        return bool(window.winfo_exists())
    except tk.TclError:
        return False


def _shell(tmp_path: Path, **cfg: Any) -> FakeShell:
    return FakeShell(Config(**cfg), tmp_path / "config.json")


def _saved(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))


# --- checkbox reflects reality on open (config flag may be stale) ---


@pytest.mark.parametrize(("present", "flag", "checked"), [
    (True, False, True),    # shortcut exists, stale flag false -> checked
    (False, True, False),   # no shortcut, stale flag true -> unchecked
    (True, True, True),
    (False, False, False),
])
def test_open_reflects_real_shortcut_not_config(
        root: tk.Tk, tmp_path: Path, fake: FakeAutostart,
        present: bool, flag: bool, checked: bool) -> None:
    fake.present = present
    form = settings.SettingsForm(_shell(tmp_path, autostart=flag), root)
    assert form.autostart.get() is checked
    assert fake.exists_calls, "open must inspect the real shortcut"
    assert fake.create_calls == [] and fake.remove_calls == []


# --- Apply matrix: create / remove / no-op, config stays in sync ---


def test_apply_creates_shortcut_when_checked_and_missing(
        root: tk.Tk, tmp_path: Path, fake: FakeAutostart) -> None:
    shell = _shell(tmp_path, autostart=False)
    form = settings.SettingsForm(shell, root)
    form.autostart.set(True)
    form.on_ok()
    assert shell.restart_event.wait(5)
    assert len(fake.create_calls) == 1
    startup_dir, pythonw, kwargs = fake.create_calls[0]
    assert startup_dir == settings._startup_dir()
    assert pythonw == PYTHONW
    assert kwargs.get("arguments") == TRAY_ARGS
    assert fake.remove_calls == []
    assert shell.restarted[0].autostart is True
    assert _saved(tmp_path)["autostart"] is True


def test_apply_removes_shortcut_when_unchecked_and_present(
        root: tk.Tk, tmp_path: Path, fake: FakeAutostart) -> None:
    fake.present = True
    shell = _shell(tmp_path, autostart=True)
    form = settings.SettingsForm(shell, root)
    form.autostart.set(False)
    form.on_ok()
    assert shell.restart_event.wait(5)
    assert fake.remove_calls == [settings._startup_dir()]
    assert fake.create_calls == []
    assert shell.restarted[0].autostart is False
    assert _saved(tmp_path)["autostart"] is False


@pytest.mark.parametrize(("present", "desired"), [(True, True), (False, False)])
def test_apply_in_sync_is_noop_but_saves_flag(
        root: tk.Tk, tmp_path: Path, fake: FakeAutostart,
        present: bool, desired: bool) -> None:
    fake.present = present
    shell = _shell(tmp_path, autostart=not desired)  # stale flag, real state
    form = settings.SettingsForm(shell, root)
    assert form.autostart.get() is desired
    form.on_ok()
    assert shell.restart_event.wait(5)
    assert fake.create_calls == [] and fake.remove_calls == []
    assert shell.restarted[0].autostart is desired
    assert _saved(tmp_path)["autostart"] is desired


def test_cancel_never_touches_shortcut_or_config(
        root: tk.Tk, tmp_path: Path, fake: FakeAutostart) -> None:
    fake.present = True
    shell = _shell(tmp_path)
    form = settings.SettingsForm(shell, root)
    form.autostart.set(False)
    form.on_cancel()
    assert not _alive(root)
    assert fake.create_calls == [] and fake.remove_calls == []
    assert shell.restarted == []
    assert not (tmp_path / "config.json").exists()


# --- create/remove failures: inline error, window stays open (#7 UX) ---


def test_create_failure_shows_inline_and_stays_open(
        root: tk.Tk, tmp_path: Path, fake: FakeAutostart) -> None:
    fake.create_error = subprocess.CalledProcessError(1, "powershell.exe")
    shell = _shell(tmp_path, autostart=False)
    form = settings.SettingsForm(shell, root)
    form.autostart.set(True)
    form.on_ok()
    assert "Autostart" in form.error.cget("text")
    assert _alive(root), "window must stay open on shortcut failure"
    assert shell.restarted == []
    assert not (tmp_path / "config.json").exists()


def test_remove_failure_shows_inline_and_stays_open(
        root: tk.Tk, tmp_path: Path, fake: FakeAutostart) -> None:
    fake.present = True
    fake.remove_error = OSError("access denied")
    shell = _shell(tmp_path)
    form = settings.SettingsForm(shell, root)
    form.autostart.set(False)
    form.on_ok()
    assert "Autostart" in form.error.cget("text")
    assert _alive(root)
    assert shell.restarted == []
    assert not (tmp_path / "config.json").exists()


# --- integration: a REAL .lnk in a tmp_path Startup dir (never the real one) ---


def test_apply_writes_real_lnk_with_tray_target(
        root: tk.Tk, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    startup = tmp_path / "Startup"
    startup.mkdir()
    monkeypatch.setattr(settings, "_startup_dir", lambda: startup)
    shell = _shell(tmp_path, autostart=False)
    form = settings.SettingsForm(shell, root)
    assert form.autostart.get() is False  # no .lnk yet
    form.autostart.set(True)
    form.on_ok()
    assert shell.restart_event.wait(5)
    lnk = startup / autostart.SHORTCUT_NAME
    assert lnk.is_file()
    target, arguments, _ = read_lnk(lnk)
    assert target == str(PYTHONW)
    assert arguments == TRAY_ARGS
    assert _saved(tmp_path)["autostart"] is True


def test_reopen_sees_real_lnk_and_uncheck_removes_it(
        root: tk.Tk, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    startup = tmp_path / "Startup"
    startup.mkdir()
    monkeypatch.setattr(settings, "_startup_dir", lambda: startup)
    autostart.create_shortcut(startup, PYTHONW, arguments=TRAY_ARGS)
    shell = _shell(tmp_path, autostart=False)  # stale flag: reality wins
    form = settings.SettingsForm(shell, root)
    assert form.autostart.get() is True
    form.autostart.set(False)
    form.on_ok()
    assert shell.restart_event.wait(5)
    assert not (startup / autostart.SHORTCUT_NAME).exists()
    assert _saved(tmp_path)["autostart"] is False


# --- micro-fix (PR #22 review): loopback detection is IP-based ---


@pytest.mark.parametrize(("address", "warns"), [
    ("127.0.0.1:8118", False),
    ("127.example.com:8118", True),  # hostname, not a 127/8 IP
    ("localhost:8118", False),
    ("0.0.0.0:8118", True),
])
def test_lan_warning_loopback_matrix(address: str, warns: bool) -> None:
    assert (settings.lan_warning([address]) is not None) is warns
