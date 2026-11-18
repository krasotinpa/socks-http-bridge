"""Settings window: the Config editor with Apply (issue #7, GLOSSARY.md).

``open_settings(shell)`` is invoked from the tray menu and runs the tkinter
window on its own thread (Tk is thread-affine). Apply saves Config and
restarts the Listeners immediately on a worker thread: active Tunnels drop,
clients reconnect on their own, and the UI never blocks. Validation reuses
config.py (``Config.validate`` / ``ConfigError``); messages show inline and
the window stays open. Autostart (issue #8): the checkbox reflects the real
Startup shortcut on open (reality beats a stale ``config.autostart``); OK
creates/removes the shortcut through the #15 core before Applying, targeting
``pythonw.exe -m socks_http_bridge --tray`` (no console at logon). Shortcut
failures show inline and keep the window open, like validation errors.
"""

import ipaddress
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from typing import TYPE_CHECKING

from socks_http_bridge import autostart
from socks_http_bridge.config import Config, ConfigError

if TYPE_CHECKING:
    from socks_http_bridge.tray import TrayShell

TRAY_ARGUMENTS = "-m socks_http_bridge --tray"


def _startup_dir() -> Path:
    """The per-user Startup folder (the #15 core takes it as a parameter)."""
    return (Path(os.environ["APPDATA"])
            / "Microsoft/Windows/Start Menu/Programs/Startup")


def sync_autostart(desired: bool) -> None:
    """Create/remove the real Startup shortcut to match ``desired``; no-op
    when already in sync. Raises OSError / CalledProcessError on failure."""
    startup = _startup_dir()
    present = autostart.shortcut_exists(startup)
    if desired and not present:
        autostart.create_shortcut(
            startup, Path(sys.executable).with_name("pythonw.exe"),
            arguments=TRAY_ARGUMENTS)
    elif present and not desired:
        autostart.remove_shortcut(startup)


def config_from_form(*, listeners: list[str], upstream: str, autostart: bool,
                     probe_interval: str, log_path: str, test_url: str) -> Config:
    """Build a validated Config from raw form strings.

    Raises ConfigError with a message safe to show inline in the window.
    """
    try:
        interval = int(probe_interval.strip())
    except ValueError:
        raise ConfigError(
            "probe_interval must be an integer in 2..300") from None
    cfg = Config(listeners=[item.strip() for item in listeners],
                 upstream=upstream.strip(), autostart=autostart,
                 probe_interval=interval, log_path=log_path.strip(),
                 test_url=test_url.strip())
    cfg.validate()
    return cfg


def lan_warning(listeners: list[str]) -> str | None:
    """Warning text when a Listener is non-loopback, else None."""
    exposed = [a for a in listeners if not _is_loopback(a)]
    if not exposed:
        return None
    return (f"Listener {', '.join(exposed)} is not loopback: "
            "other devices on your LAN can use this Bridge.")


def _is_loopback(address: str) -> bool:
    host = address.rpartition(":")[0].lower()
    if host in {"localhost", "[::1]"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname is conservatively non-loopback: warn


def apply_config(shell: "TrayShell", cfg: Config,
                 path: Path | None = None) -> None:
    """Apply: save Config, then restart the Listeners immediately.

    Runs on a worker thread (the restart may join the Probe for up to ~3 s).
    Bind failures follow the tray Start pattern: log + Bridge State Error.
    """
    cfg.save(path)
    shell.restart_listeners(cfg)


def open_settings(shell: "TrayShell") -> None:
    """Open Settings on a dedicated thread: Tk is thread-affine, and the
    tray menu callback must not block on the window's mainloop."""
    threading.Thread(target=_run_window, args=(shell,), daemon=True).start()


def _run_window(shell: "TrayShell") -> None:
    root = tk.Tk()
    SettingsForm(shell, root)
    root.mainloop()  # returns once OK/Cancel (or the X button) destroys root


class SettingsForm:
    """The Settings widgets on a Tk root; no mainloop, so tests drive it.

    OK validates, then Applies on a worker thread and closes; validation
    errors show inline and the window stays open. Cancel discards.
    """

    def __init__(self, shell: "TrayShell", root: tk.Tk) -> None:
        self._shell = shell
        self._root = root
        cfg = shell.config
        root.title("Socks HTTP Bridge — Settings")
        root.columnconfigure(0, weight=1)

        tk.Label(root, text="Listeners (host:port)").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
        self.listeners = tk.Listbox(root, height=4, exportselection=False)
        for address in cfg.listeners:
            self.listeners.insert(tk.END, address)
        self.listeners.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8)
        self.listener_entry = tk.Entry(root)
        self.listener_entry.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        tk.Button(root, text="Add", command=self.add_listener).grid(
            row=2, column=1, padx=4)
        tk.Button(root, text="Remove", command=self.remove_listener).grid(
            row=2, column=2, padx=(4, 8))

        self.upstream = self._field(root, 3, "Upstream (SOCKS5)", cfg.upstream)
        # Reality beats a stale config.autostart: check the real shortcut.
        self.autostart = tk.BooleanVar(
            value=autostart.shortcut_exists(_startup_dir()))
        tk.Checkbutton(root, text="Start with Windows (Autostart)",
                       variable=self.autostart).grid(
            row=4, column=0, columnspan=3, sticky="w", padx=8)
        self.probe_interval = self._field(
            root, 5, "Probe interval (2..300 s)", str(cfg.probe_interval))
        self.log_path = self._field(root, 6, "Log path", cfg.log_path)
        self.test_url = self._field(root, 7, "Exit IP test URL", cfg.test_url)

        self.warning = tk.Label(root, fg="#b05000", wraplength=420,
                                justify="left")
        self.warning.grid(row=8, column=0, columnspan=3, sticky="w", padx=8)
        self.error = tk.Label(root, fg="#c00000", wraplength=420,
                              justify="left")
        self.error.grid(row=9, column=0, columnspan=3, sticky="w", padx=8)
        tk.Button(root, text="OK", command=self.on_ok).grid(
            row=10, column=1, pady=8)
        tk.Button(root, text="Cancel", command=self.on_cancel).grid(
            row=10, column=2, padx=(4, 8))
        self._refresh_warning()

    @staticmethod
    def _field(root: tk.Tk, row: int, label: str, value: str) -> tk.Entry:
        tk.Label(root, text=label).grid(row=row, column=0, sticky="w",
                                        padx=8, pady=2)
        entry = tk.Entry(root)
        entry.insert(0, value)
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 8))
        return entry

    def add_listener(self) -> None:
        address = self.listener_entry.get().strip()
        if address:
            self.listeners.insert(tk.END, address)
            self.listener_entry.delete(0, tk.END)
            self._refresh_warning()

    def remove_listener(self) -> None:
        for index in reversed(self.listeners.curselection()):
            self.listeners.delete(index)
        self._refresh_warning()

    def _refresh_warning(self) -> None:
        text = lan_warning(list(self.listeners.get(0, tk.END)))
        self.warning.config(text=text or "")

    def on_ok(self) -> None:
        """Apply + close; a ConfigError keeps the window open, inline."""
        try:
            cfg = config_from_form(
                listeners=list(self.listeners.get(0, tk.END)),
                upstream=self.upstream.get(), autostart=self.autostart.get(),
                probe_interval=self.probe_interval.get(),
                log_path=self.log_path.get(), test_url=self.test_url.get())
        except ConfigError as exc:
            self.error.config(text=str(exc))
            return
        try:
            # Before Apply/close so a shortcut failure shows inline (#7 UX).
            sync_autostart(cfg.autostart)
        except (OSError, subprocess.CalledProcessError) as exc:
            self.error.config(text=f"Cannot update the Autostart shortcut: {exc}")
            return
        self.error.config(text="")
        threading.Thread(target=self._apply_worker, args=(cfg,),
                         daemon=True).start()
        self._root.destroy()

    def _apply_worker(self, cfg: Config) -> None:
        try:
            apply_config(self._shell, cfg)
        except OSError as exc:
            # The window is already closed; the balloon is the only seam left.
            self._shell.notify(f"Cannot save settings: {exc}")

    def on_cancel(self) -> None:
        """Discard: nothing saved, nothing restarted."""
        self._root.destroy()
