"""Tests for the Autostart Startup-shortcut core.

Every shortcut lives in a pytest tmp_path directory -- the real Startup
folder is never touched. Target/arguments are verified by reading the
.lnk back through an independent PowerShell WScript.Shell call.
"""

import subprocess
import sys
from pathlib import Path

from socks_http_bridge import autostart

PYTHONW = Path(sys.executable).with_name("pythonw.exe")


def read_lnk(lnk: Path) -> tuple[str, str, str]:
    """Read TargetPath/Arguments/WorkingDirectory back via WScript.Shell."""
    quoted = str(lnk).replace("'", "''")
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{quoted}'); "
        "Write-Output $s.TargetPath; Write-Output $s.Arguments; "
        "Write-Output $s.WorkingDirectory"
    )
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    target, arguments, working_dir = out.splitlines()
    return target, arguments, working_dir


def test_create_writes_shortcut_with_target_and_arguments(tmp_path: Path) -> None:
    lnk = autostart.create_shortcut(tmp_path, PYTHONW)
    assert lnk == tmp_path / "SocksHttpBridge.lnk"
    assert lnk.is_file()
    target, arguments, _ = read_lnk(lnk)
    assert target == str(PYTHONW)
    assert arguments == "-m socks_http_bridge"


def test_create_is_idempotent(tmp_path: Path) -> None:
    first = autostart.create_shortcut(tmp_path, PYTHONW)
    second = autostart.create_shortcut(tmp_path, PYTHONW)
    assert first == second
    assert list(tmp_path.iterdir()) == [first]
    target, arguments, _ = read_lnk(first)
    assert target == str(PYTHONW)
    assert arguments == "-m socks_http_bridge"


def test_create_overwrites_stale_shortcut(tmp_path: Path) -> None:
    autostart.create_shortcut(tmp_path, PYTHONW, arguments="-m old_entry")
    lnk = autostart.create_shortcut(tmp_path, PYTHONW)
    _, arguments, _ = read_lnk(lnk)
    assert arguments == "-m socks_http_bridge"


def test_create_working_directory_default_and_override(tmp_path: Path) -> None:
    lnk = autostart.create_shortcut(tmp_path, PYTHONW)
    _, _, default_dir = read_lnk(lnk)
    assert default_dir == str(PYTHONW.parent)
    work = tmp_path / "work dir"
    work.mkdir()
    autostart.create_shortcut(tmp_path, PYTHONW, working_dir=work)
    _, _, overridden = read_lnk(lnk)
    assert overridden == str(work)


def test_create_handles_spaces_and_quotes_in_paths(tmp_path: Path) -> None:
    startup = tmp_path / "it's a Startup dir"
    startup.mkdir()
    lnk = autostart.create_shortcut(startup, PYTHONW)
    assert lnk.is_file()
    target, arguments, _ = read_lnk(lnk)
    assert target == str(PYTHONW)
    assert arguments == "-m socks_http_bridge"


def test_remove_deletes_shortcut(tmp_path: Path) -> None:
    lnk = autostart.create_shortcut(tmp_path, PYTHONW)
    autostart.remove_shortcut(tmp_path)
    assert not lnk.exists()


def test_remove_is_idempotent(tmp_path: Path) -> None:
    autostart.remove_shortcut(tmp_path)
    autostart.create_shortcut(tmp_path, PYTHONW)
    autostart.remove_shortcut(tmp_path)
    autostart.remove_shortcut(tmp_path)
    assert not autostart.shortcut_exists(tmp_path)


def test_exists_reflects_reality(tmp_path: Path) -> None:
    assert not autostart.shortcut_exists(tmp_path)
    autostart.create_shortcut(tmp_path, PYTHONW)
    assert autostart.shortcut_exists(tmp_path)
    autostart.remove_shortcut(tmp_path)
    assert not autostart.shortcut_exists(tmp_path)


def test_inspect_matches_created_shortcut(tmp_path: Path) -> None:
    assert autostart.inspect_shortcut(tmp_path) is None
    work = tmp_path / "work"
    work.mkdir()
    autostart.create_shortcut(tmp_path, PYTHONW, working_dir=work)
    info = autostart.inspect_shortcut(tmp_path)
    assert info is not None
    assert info.target == str(PYTHONW)
    assert info.arguments == "-m socks_http_bridge"
    assert info.working_directory == str(work)
    assert autostart.inspect_shortcut(tmp_path) == read_lnk(
        tmp_path / "SocksHttpBridge.lnk")
    autostart.remove_shortcut(tmp_path)
    assert autostart.inspect_shortcut(tmp_path) is None
