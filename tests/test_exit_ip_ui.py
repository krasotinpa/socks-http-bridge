"""Exit IP Test UI tests: pure result/error formatting, worker orchestration
with a fake fetch and fake popup at the module seams, and Tk construction
without a real mainloop. No network, no visible windows; the live Bridge
and the live Upstream are never touched (the fake Listeners use port 18118;
nothing binds or dials it).
"""

import threading
import time
import tkinter as tk

import pytest

from socks_http_bridge import exit_ip, exit_ip_ui
from socks_http_bridge.config import Config
from socks_http_bridge.exit_ip import ExitIpError, ExitIpResult


def _wait_worker_idle(timeout: float = 5.0) -> None:
    # The worker clears _active itself on exit; Thread.is_alive() alone is
    # racy (a dying thread can leave enumerate() before it turns False).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if exit_ip_ui._active is None:
            return
        time.sleep(0.01)
    raise AssertionError("Exit IP Test worker still running")


@pytest.fixture(autouse=True)
def _worker_idle() -> None:
    yield
    _wait_worker_idle()


class FakeShell:
    """The TrayShell seam the feature module uses: config,
    listener_addresses, notify."""

    def __init__(self, addresses: list[tuple[str, int]],
                 test_url: str = "https://exit.test/json") -> None:
        self.config = Config(test_url=test_url)
        self._addresses = addresses
        self.notifications: list[tuple[str, str]] = []
        self.notified = threading.Event()

    @property
    def listener_addresses(self) -> list[tuple[str, int]]:
        return list(self._addresses)

    def notify(self, message: str, title: str = "Socks HTTP Bridge") -> None:
        self.notifications.append((message, title))
        self.notified.set()


def test_format_result_shows_ip_country_city_org() -> None:
    result = ExitIpResult(ip="203.0.113.7", country="FI", city="Helsinki",
                          org="AS12345 Example VPN")
    assert exit_ip_ui.format_result(result) == (
        "Exit IP: 203.0.113.7\n"
        "Country: FI\n"
        "City: Helsinki\n"
        "ASN/org: AS12345 Example VPN")


def test_format_result_marks_missing_geo_unknown() -> None:
    text = exit_ip_ui.format_result(ExitIpResult(ip="203.0.113.7"))
    assert "Exit IP: 203.0.113.7" in text
    assert "Country: unknown" in text
    assert "City: unknown" in text
    assert "ASN/org: unknown" in text


def test_format_error_shows_exit_ip_error_message_as_is() -> None:
    message = ("Exit IP Test request to https://exit.test/json failed: "
               "timed out")
    assert exit_ip_ui.format_error(ExitIpError(message)) == message


def test_format_error_unexpected_exception_stays_readable() -> None:
    text = exit_ip_ui.format_error(RuntimeError("boom"))
    assert "Exit IP Test" in text
    assert "boom" in text
    assert "Traceback" not in text


class PopupRecorder:
    """Fake popup seam: records texts, signals completion."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.done = threading.Event()

    def __call__(self, text: str) -> None:
        self.texts.append(text)
        self.done.set()


def test_menu_fetches_via_first_listener_off_ui_thread_and_popups_result(
        monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell([("127.0.0.1", 18118), ("127.0.0.1", 18119)],
                      test_url="https://exit.test/json")
    caller = threading.current_thread()
    calls: list[tuple[tuple[str, int], str, float, threading.Thread]] = []

    def fake_fetch(proxy_address: tuple[str, int], test_url: str,
                   timeout: float = 10.0) -> ExitIpResult:
        calls.append((proxy_address, test_url, timeout,
                      threading.current_thread()))
        return ExitIpResult(ip="203.0.113.7", country="FI", city="Helsinki",
                            org="AS12345 Example VPN")

    popup = PopupRecorder()
    monkeypatch.setattr(exit_ip, "fetch_exit_ip", fake_fetch)
    monkeypatch.setattr(exit_ip_ui, "show_popup", popup)
    exit_ip_ui.run_test(shell)
    assert popup.done.wait(5)
    assert len(calls) == 1
    address, url, timeout, worker = calls[0]
    assert address == ("127.0.0.1", 18118)  # the first bound Listener
    assert url == "https://exit.test/json"  # Config test_url
    assert timeout == 10.0
    assert worker is not caller  # fetch runs off the UI thread
    assert popup.texts == [
        ("Exit IP: 203.0.113.7\nCountry: FI\nCity: Helsinki\n"
         "ASN/org: AS12345 Example VPN")]
    assert shell.notifications == []


@pytest.mark.parametrize(("wildcard", "loopback"), [
    ("0.0.0.0", "127.0.0.1"),
    ("::", "::1"),
])
def test_wildcard_listener_is_normalized_to_loopback_for_the_fetch(
        monkeypatch: pytest.MonkeyPatch, wildcard: str, loopback: str) -> None:
    # connect() to a wildcard bind address fails on Windows (WinError
    # 10049); the Exit IP Test must dial the loopback equivalent instead.
    shell = FakeShell([(wildcard, 18118)])
    calls: list[tuple[str, int]] = []

    def fake_fetch(proxy_address: tuple[str, int], test_url: str,
                   timeout: float = 10.0) -> ExitIpResult:
        calls.append(proxy_address)
        return ExitIpResult(ip="203.0.113.7")

    popup = PopupRecorder()
    monkeypatch.setattr(exit_ip, "fetch_exit_ip", fake_fetch)
    monkeypatch.setattr(exit_ip_ui, "show_popup", popup)
    exit_ip_ui.run_test(shell)
    assert popup.done.wait(5)
    assert calls == [(loopback, 18118)]


def test_run_test_returns_while_fetch_runs_and_reclick_is_ignored(
        monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell([("127.0.0.1", 18118)])
    started = threading.Event()
    release = threading.Event()
    fetch_count = 0

    def slow_fetch(*args: object, **kwargs: object) -> ExitIpResult:
        nonlocal fetch_count
        fetch_count += 1
        started.set()
        assert release.wait(5)
        return ExitIpResult(ip="203.0.113.7")

    popup = PopupRecorder()
    monkeypatch.setattr(exit_ip, "fetch_exit_ip", slow_fetch)
    monkeypatch.setattr(exit_ip_ui, "show_popup", popup)
    exit_ip_ui.run_test(shell)  # returns while the fetch is still blocked
    assert started.wait(5)
    exit_ip_ui.run_test(shell)  # re-click while active: no second window
    assert fetch_count == 1
    release.set()
    assert popup.done.wait(5)
    assert len(popup.texts) == 1
    # Once the popup is closed a new test may run again.
    _wait_worker_idle()
    popup.done.clear()
    exit_ip_ui.run_test(shell)
    assert popup.done.wait(5)
    assert fetch_count == 2


def test_dead_upstream_shows_readable_error_in_popup(
        monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell([("127.0.0.1", 18118)])
    message = ("Exit IP Test request to https://exit.test/json failed: "
               "[WinError 10061] No connection could be made because the "
               "target machine actively refused it")

    def dead_fetch(*args: object, **kwargs: object) -> ExitIpResult:
        raise ExitIpError(message)

    popup = PopupRecorder()
    monkeypatch.setattr(exit_ip, "fetch_exit_ip", dead_fetch)
    monkeypatch.setattr(exit_ip_ui, "show_popup", popup)
    exit_ip_ui.run_test(shell)
    assert popup.done.wait(5)
    assert popup.texts == [message]
    assert shell.notifications == []


def test_fetch_timeout_shows_readable_error_in_popup(
        monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell([("127.0.0.1", 18118)])

    def timeout_fetch(*args: object, **kwargs: object) -> ExitIpResult:
        raise ExitIpError("Exit IP Test request to https://exit.test/json "
                          "failed: The read operation timed out")

    popup = PopupRecorder()
    monkeypatch.setattr(exit_ip, "fetch_exit_ip", timeout_fetch)
    monkeypatch.setattr(exit_ip_ui, "show_popup", popup)
    exit_ip_ui.run_test(shell)
    assert popup.done.wait(5)
    assert "timed out" in popup.texts[0]


def test_unexpected_fetch_failure_still_reaches_popup(
        monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell([("127.0.0.1", 18118)])

    def buggy_fetch(*args: object, **kwargs: object) -> ExitIpResult:
        raise RuntimeError("boom")

    popup = PopupRecorder()
    monkeypatch.setattr(exit_ip, "fetch_exit_ip", buggy_fetch)
    monkeypatch.setattr(exit_ip_ui, "show_popup", popup)
    exit_ip_ui.run_test(shell)
    assert popup.done.wait(5)
    assert "boom" in popup.texts[0]
    assert "Exit IP Test" in popup.texts[0]


def test_stopped_bridge_notifies_and_never_fetches(
        monkeypatch: pytest.MonkeyPatch) -> None:
    shell = FakeShell([])  # no bound Listener while the Bridge is Stopped

    def no_fetch(*args: object, **kwargs: object) -> ExitIpResult:
        raise AssertionError("fetch must not run without a Listener")

    popup = PopupRecorder()
    monkeypatch.setattr(exit_ip, "fetch_exit_ip", no_fetch)
    monkeypatch.setattr(exit_ip_ui, "show_popup", popup)
    exit_ip_ui.run_test(shell)
    assert shell.notified.wait(5)
    assert len(shell.notifications) == 1
    assert "Listener" in shell.notifications[0][0]
    assert popup.texts == []


def test_show_popup_builds_tk_window_without_running_mainloop(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # The real Tk construction runs, but mainloop is replaced: the window
    # stays withdrawn (deiconify is scheduled on the loop) and is destroyed
    # at once, so no window ever becomes visible and nothing blocks.
    seen: list[tuple[str, list[str]]] = []

    def fake_mainloop(root: tk.Tk) -> None:
        texts = [w.cget("text") for w in root.winfo_children()]
        seen.append((root.title(), texts))
        root.destroy()

    monkeypatch.setattr(tk.Tk, "mainloop", fake_mainloop)
    exit_ip_ui.show_popup("Exit IP: 203.0.113.7\nCountry: FI")
    assert len(seen) == 1
    title, texts = seen[0]
    assert title == "Exit IP Test"
    assert "Exit IP: 203.0.113.7\nCountry: FI" in texts
    assert "OK" in texts