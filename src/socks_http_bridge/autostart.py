"""Autostart core: manage the Bridge's Startup shortcut (no tray/UI).

Creates/removes/inspects ``SocksHttpBridge.lnk`` in a *given* Startup
directory, targeting ``pythonw.exe -m socks_http_bridge`` so the Bridge
starts at Windows logon without a console window. Create and remove are
idempotent. The .lnk is written through PowerShell ``WScript.Shell`` COM
(stdlib subprocess only -- no pywin32), with the console hidden.
"""

import subprocess
from pathlib import Path
from typing import NamedTuple

SHORTCUT_NAME = "SocksHttpBridge.lnk"
DEFAULT_ARGUMENTS = "-m socks_http_bridge"


class ShortcutInfo(NamedTuple):
    target: str
    arguments: str
    working_directory: str


def _ps_quote(value: str) -> str:
    """Single-quoted PowerShell string literal (spaces safe, ' doubled)."""
    return "'" + value.replace("'", "''") + "'"


def _run_ps(script: str) -> str:
    # CREATE_NO_WINDOW + SW_HIDE: never flash a console from a tray app.
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script],
        capture_output=True, text=True, encoding="utf-8", check=True,
        creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startupinfo,
    )
    return result.stdout


def create_shortcut(
    startup_dir: Path,
    pythonw: Path,
    *,
    arguments: str = DEFAULT_ARGUMENTS,
    working_dir: Path | None = None,
) -> Path:
    """Create (or overwrite) the Startup shortcut; return its path.

    Idempotent: saving over an existing shortcut yields the same result.
    Working directory defaults to the directory holding ``pythonw``.
    """
    lnk = startup_dir / SHORTCUT_NAME
    work = working_dir if working_dir is not None else pythonw.parent
    _run_ps(
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(lnk))}); "
        f"$s.TargetPath = {_ps_quote(str(pythonw))}; "
        f"$s.Arguments = {_ps_quote(arguments)}; "
        f"$s.WorkingDirectory = {_ps_quote(str(work))}; "
        "$s.Save()"
    )
    return lnk


def remove_shortcut(startup_dir: Path) -> None:
    """Delete the Startup shortcut if present; idempotent."""
    (startup_dir / SHORTCUT_NAME).unlink(missing_ok=True)


def shortcut_exists(startup_dir: Path) -> bool:
    return (startup_dir / SHORTCUT_NAME).is_file()


def inspect_shortcut(startup_dir: Path) -> ShortcutInfo | None:
    """Read the shortcut's target/arguments/working directory, or None."""
    lnk = startup_dir / SHORTCUT_NAME
    if not lnk.is_file():
        return None
    out = _run_ps(
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(lnk))}); "
        "Write-Output $s.TargetPath; Write-Output $s.Arguments; "
        "Write-Output $s.WorkingDirectory"
    )
    target, arguments, working_directory = out.splitlines()
    return ShortcutInfo(target, arguments, working_directory)
