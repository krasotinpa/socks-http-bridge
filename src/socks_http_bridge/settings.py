"""Settings window: menu hook, stub until issue #7 owns this module.

Seam for #7: ``open_settings(shell)`` is invoked from the tray menu. The
shell offers ``shell.config``, ``shell.notify(...)`` and
``stop_listeners()``/``start_listeners()`` for Apply (restart the Listeners
immediately; active Tunnels drop and clients reconnect on their own).
Keep the feature logic here, out of the Tray Shell.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from socks_http_bridge.tray import TrayShell


def open_settings(shell: "TrayShell") -> None:
    """Stub balloon; chosen over a disabled item so the affordance stays
    visible and #7 only replaces this body."""
    shell.notify("Settings is not implemented yet")
