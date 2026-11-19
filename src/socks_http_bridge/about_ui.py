"""About UI: tray menu hook and read-only popup (issue #35).

Seam from the Tray Shell: ``show_about(shell)`` is invoked from the menu.
The popup shows the application name, the installed version (package
metadata is the single source of truth), the license line and a clickable
repository link. Same threading arrangement as exit_ip_ui: the menu action
dispatches a daemon worker that builds and drives the Tk popup on its own
thread; re-clicks while a popup is open are ignored.
"""

import importlib.metadata
import os
import threading
import tkinter as tk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from socks_http_bridge.tray import TrayShell

REPO_URL = "https://github.com/krasotinpa/socks-http-bridge"

_WORKER_NAME = "about"
_lock = threading.Lock()
_active: threading.Thread | None = None


def about_text() -> str:
    """Popup body: name, installed version, license."""
    version = importlib.metadata.version("socks-http-bridge")
    return ("Socks HTTP Bridge\n"
            f"Version {version}\n"
            "License: MIT")


def show_about(shell: "TrayShell") -> None:
    """Menu action: dispatch the popup off the UI thread and return at once.

    Re-clicks while a popup is open are ignored (exit_ip_ui pattern): at
    most one About window at a time. ``_active`` is cleared by the worker
    itself on exit, not inferred from Thread.is_alive().
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
        show_popup(shell)
    finally:
        with _lock:
            _active = None


def show_popup(shell: "TrayShell") -> None:
    """Build the About window and block until the user closes it.

    Runs on the worker thread only (tkinter is not thread-safe across
    threads). Built withdrawn and deiconified by the event loop, so tests
    can replace mainloop without a visible window (exit_ip_ui pattern).
    """
    root = tk.Tk()
    root.withdraw()
    root.title("Socks HTTP Bridge — About")
    root.attributes("-topmost", True)
    tk.Label(root, text=about_text(), justify="left", padx=16, pady=12).pack()
    link = tk.Label(root, text=REPO_URL, fg="blue", cursor="hand2", padx=16)
    link.pack()
    link.bind("<Button-1>", lambda event: open_repo(shell))
    tk.Button(root, text="OK", width=10, command=root.destroy).pack(
        pady=(0, 12))
    root.after(0, root.deiconify)
    root.mainloop()


def open_repo(shell: "TrayShell") -> None:
    """Open the repository in the default browser; failures balloon."""
    try:
        os.startfile(REPO_URL)
    except OSError as exc:
        shell.notify(f"Cannot open repository: {exc}")
