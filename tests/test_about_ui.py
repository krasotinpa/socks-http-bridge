"""About UI tests: popup content at the Tk seam (real construction, no
mainloop, no visible windows), the repository link action, and worker
orchestration with a fake popup. No network; the live Bridge and the live
Upstream are never touched.
"""

import importlib.metadata
import threading
import time
import tkinter as tk

import pytest

from socks_http_bridge import about_ui

# Independent truth: the repository URL, not the implementation constant.
_REPO = "https://github.com/krasotinpa/socks-http-bridge"


class FakeShell:
    """The TrayShell seam About uses: notify."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []

    def notify(self, message: str, title: str = "Socks HTTP Bridge") -> None:
        self.notifications.append((message, title))


def _wait_worker_idle(timeout: float = 5.0) -> None:
    # The worker clears _active itself on exit; Thread.is_alive() alone is
    # racy (a dying thread can leave enumerate() before it turns False).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if about_ui._active is None:
            return
        time.sleep(0.01)
    raise AssertionError("About worker still running")


@pytest.fixture(autouse=True)
def _worker_idle() -> None:
    yield
    _wait_worker_idle()


def test_show_popup_builds_tk_window_with_name_version_license(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # The real Tk construction runs, but mainloop is replaced: the window
    # stays withdrawn and is destroyed at once (same seam as exit_ip_ui).
    seen: list[tuple[str, list[str], bool]] = []

    def fake_mainloop(root: tk.Tk) -> None:
        widgets = root.winfo_children()
        texts = [w.cget("text") for w in widgets]
        link_bound = any(w.cget("text") == _REPO and w.bind("<Button-1>")
                         for w in widgets)
        seen.append((root.title(), texts, link_bound))
        root.destroy()

    monkeypatch.setattr(tk.Tk, "mainloop", fake_mainloop)
    about_ui.show_popup(FakeShell())
    assert len(seen) == 1
    title, texts, link_bound = seen[0]
    assert title == "Socks HTTP Bridge — About"
    # The version assertion reads the real installed package metadata —
    # the single source of truth, never a copied constant (issue #35).
    version = importlib.metadata.version("socks-http-bridge")
    assert f"Socks HTTP Bridge\nVersion {version}\nLicense: MIT" in texts
    assert _REPO in texts  # the repository link is shown
    assert link_bound  # ...and clickable
    assert "OK" in texts


def test_open_repo_opens_url_in_browser_and_reports_failure(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct seam, like tray.open_log: the default browser gets the repo
    # URL; a missing browser is reported through the balloon, never raised.
    # Synthetic Tk button events need a viewable window (Windows Tk), so
    # the click itself cannot run hermetically; the popup test above
    # checks that the link carries the <Button-1> binding.
    shell = FakeShell()
    opened: list[str] = []
    monkeypatch.setattr(about_ui.os, "startfile", opened.append,
                        raising=False)
    about_ui.open_repo(shell)
    assert opened == [_REPO]

    def no_browser(url: str) -> None:
        raise OSError("no browser")

    monkeypatch.setattr(about_ui.os, "startfile", no_browser, raising=False)
    about_ui.open_repo(shell)
    assert "no browser" in shell.notifications[-1][0]


def test_menu_action_returns_at_once_and_reclick_is_ignored(
        monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell()
    caller = threading.current_thread()
    started = threading.Event()
    release = threading.Event()
    pops: list[threading.Thread] = []

    def slow_popup(sh: FakeShell) -> None:
        pops.append(threading.current_thread())
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(about_ui, "show_popup", slow_popup)
    about_ui.show_about(shell)  # returns while the popup is still open
    assert started.wait(5)
    about_ui.show_about(shell)  # re-click while open: no second window
    assert len(pops) == 1
    assert pops[0] is not caller  # the popup runs off the UI thread
    release.set()
    _wait_worker_idle()
    started.clear()
    about_ui.show_about(shell)  # once closed, About may open again
    assert started.wait(5)
    assert len(pops) == 2


def test_worker_failure_reaches_the_user_as_a_balloon(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # exit_ip_ui pattern: a bare raise in a daemon thread would vanish
    # under pythonw; a broken popup must still give the user feedback.
    shell = FakeShell()

    def broken_popup(sh: FakeShell) -> None:
        raise RuntimeError("Tk exploded")

    monkeypatch.setattr(about_ui, "show_popup", broken_popup)
    about_ui.show_about(shell)
    _wait_worker_idle()
    assert len(shell.notifications) == 1
    assert "Tk exploded" in shell.notifications[0][0]
