"""Exit IP Test UI: tray menu hook, worker thread and result popup.

Seam from the Tray Shell (issue #5): ``run_test(shell)`` is invoked from the
menu. The shell offers ``shell.config`` (``test_url``),
``shell.listener_addresses`` (the bound Listeners; the test goes through the
first one as an HTTP proxy) and ``shell.notify(message, title)`` for balloons.

Threading: the menu action only dispatches a daemon worker thread, so the
tray menu and Probe never block. The worker fetches via the #14 core and then
creates the Tk popup on that same thread: tkinter is not thread-safe across
threads, but Windows allows one Tk root per thread, so fetch+popup in one
worker is the smallest safe arrangement (no dedicated UI thread or queue).
Re-clicks while a worker is active (fetch running or popup open) are ignored:
at most one popup at a time.
"""

import threading
import tkinter as tk
from typing import TYPE_CHECKING

from socks_http_bridge import exit_ip

if TYPE_CHECKING:
    from socks_http_bridge.tray import TrayShell

_WORKER_NAME = "exit-ip-test"
_lock = threading.Lock()
_active: threading.Thread | None = None


def format_result(result: exit_ip.ExitIpResult) -> str:
    """Popup text for a successful test; absent geo fields read 'unknown'."""
    return (f"Exit IP: {result.ip}\n"
            f"Country: {result.country or 'unknown'}\n"
            f"City: {result.city or 'unknown'}\n"
            f"ASN/org: {result.org or 'unknown'}")


def format_error(exc: Exception) -> str:
    """Popup text for a failed test; ExitIpError messages are safe as-is."""
    if isinstance(exc, exit_ip.ExitIpError):
        return str(exc)
    return f"Exit IP Test failed: {exc}"


def run_test(shell: "TrayShell") -> None:
    """Menu action: dispatch the test off the UI thread and return at once.

    Re-clicks while a worker is active (fetch running or popup open) are
    ignored, so at most one popup exists at a time. ``_active`` is cleared by
    the worker itself on exit, not inferred from Thread.is_alive(): a dying
    thread can leave enumerate() before is_alive() turns False.
    """
    global _active
    with _lock:
        if _active is not None:
            return
        _active = threading.Thread(target=_worker, args=(shell,),
                                   name=_WORKER_NAME, daemon=True)
        _active.start()


def _worker(shell: "TrayShell") -> None:
    global _active
    try:
        addresses = shell.listener_addresses
        if not addresses:
            shell.notify("Exit IP Test: no Listener is bound; "
                         "start the Bridge first")
            return
        try:
            result = exit_ip.fetch_exit_ip(
                addresses[0], shell.config.test_url,
                timeout=exit_ip.DEFAULT_TIMEOUT)
        # Any failure must reach the popup; a bare raise in a daemon thread
        # would vanish under pythonw and leave the user no feedback.
        except Exception as exc:  # noqa: BLE001
            show_popup(format_error(exc))
            return
        show_popup(format_result(result))
    finally:
        with _lock:
            _active = None


def show_popup(text: str) -> None:
    """Show the result/error popup and block until the user closes it.

    Runs on the worker thread only: the Tk root is created, driven and
    destroyed there (tkinter is not thread-safe across threads). The window
    is built withdrawn and deiconified by the event loop, so it never shows
    half-drawn — and tests can replace mainloop without a visible window.
    """
    root = tk.Tk()
    root.withdraw()
    root.title("Exit IP Test")
    root.attributes("-topmost", True)  # surface above windows of other apps
    tk.Label(root, text=text, justify="left", padx=16, pady=12).pack()
    tk.Button(root, text="OK", width=10, command=root.destroy).pack(
        pady=(0, 12))
    root.after(0, root.deiconify)
    root.mainloop()
