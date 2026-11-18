"""Exit IP Test UI: menu hook, stub until issue #6 owns this module.

Seam for #6: ``run_test(shell)`` is invoked from the tray menu. The shell
offers ``shell.config`` (``test_url``), ``shell.listener_addresses`` (the
bound Listeners; the test goes through one as an HTTP proxy) and
``shell.notify(message, title)`` for balloons. Keep the feature logic here,
out of the Tray Shell.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from socks_http_bridge.tray import TrayShell


def run_test(shell: "TrayShell") -> None:
    """Stub balloon; chosen over a disabled item so the affordance stays
    visible and #6 only replaces this body."""
    shell.notify("Exit IP Test is not implemented yet")
