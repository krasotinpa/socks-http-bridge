"""Idempotent installer for the tray Bridge (issue #9).

Run from the repo root:  uv run python scripts/install.py [--dry-run]

Steps, in this order:
  1. uv sync -- dependencies first, so a broken sync aborts before the
     Startup shortcut is touched or the tray is launched.
  2. Create the Startup shortcut SocksHttpBridge.lnk through the
     autostart core (pythonw.exe -m socks_http_bridge --tray).
     Already correct -> skip. Other .lnk files are never touched.
  3. Launch the tray detached (DETACHED_PROCESS | CREATE_NO_WINDOW).
     A tray already running is left alone: the single-instance mutex
     would exit a second launch anyway.
  4. Verify: TCP-connect the first configured Listener (default
     127.0.0.1:8118) for up to ~10 s; on failure print the log path.

Tool discovery: uv via UV_BIN env var, then shutil.which("uv"), then
the install-manager layout under %LOCALAPPDATA%\\Python (newest
pythoncore-* wins). uv sync gets --python KNOWN_PYTHON when that shim
exists (uv may not find the install-manager Python on its own, see
AGENTS.md).

Idempotent: shortcut already correct -> skip; tray already running ->
skip launch; Listener already answering with the tray up -> "Already
installed" report. --dry-run prints the plan using read-only discovery
only (no sync, writes or launch). No running process is ever stopped.

Stdlib + the autostart core only. Reuses settings._startup_dir() and
settings.TRAY_ARGUMENTS (importing settings has no side effects).
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from typing import NamedTuple

from socks_http_bridge import autostart
from socks_http_bridge.config import Config, ConfigError, connect_target, parse_address
from socks_http_bridge.settings import TRAY_ARGUMENTS, _startup_dir

# Per-user install-manager layout (%LOCALAPPDATA%\Python\...); the default
# `python` may be a store alias without pip, hence the explicit shim.
LOCAL_APP_DATA = Path(
    os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
KNOWN_PYTHON = LOCAL_APP_DATA / "Python" / "bin" / "python.exe"

PS_ENUMERATE = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "$procs = Get-CimInstance Win32_Process | "
    "Where-Object { $_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe' } "
    "| Select-Object ProcessId, CommandLine; "
    "ConvertTo-Json -InputObject @($procs) -Compress"
)


class InstallError(Exception):
    """Fatal install problem; the message is safe to show the user."""


class Proc(NamedTuple):
    pid: int
    commandline: str


def _pythoncore_version(uv: Path) -> tuple[int, ...]:
    """(3, 15) for .../pythoncore-3.15-64/Scripts/uv.exe; (0,) if odd."""
    name = uv.parents[1].name.removeprefix("pythoncore-").split("-")[0]
    try:
        return tuple(int(part) for part in name.split("."))
    except ValueError:
        return (0,)


def find_uv() -> str:
    """UV_BIN env var -> PATH -> install-manager uv under LOCAL_APP_DATA."""
    env = os.environ.get("UV_BIN")
    if env:
        return env
    found = shutil.which("uv")
    if found:
        return found
    matches = sorted(
        LOCAL_APP_DATA.glob("Python/pythoncore-*/Scripts/uv.exe"),
        key=_pythoncore_version, reverse=True)
    if matches:
        return str(matches[0])
    raise InstallError("uv not found; set UV_BIN or add uv to PATH")


def run(cmd: list[str], *,
        cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """The subprocess-runner seam: every external command goes through
    here, console hidden. Callers check returncode themselves."""
    return subprocess.run(
        cmd, check=False, capture_output=True, text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW, cwd=cwd)


def enumerate_processes() -> list[Proc]:
    """python/pythonw processes via PowerShell CIM (read-only)."""
    result = run(["powershell.exe", "-NoProfile", "-NonInteractive",
                  "-Command", PS_ENUMERATE])
    if result.returncode != 0:
        raise InstallError(
            f"process enumeration failed: {result.stderr.strip()}")
    items = json.loads(result.stdout or "[]")
    if isinstance(items, dict):  # defensive: a lone object, not a list
        items = [items]
    return [Proc(int(p["ProcessId"]), p.get("CommandLine") or "")
            for p in items]


def is_tray(commandline: str) -> bool:
    return "-m socks_http_bridge" in commandline and "--tray" in commandline


def shortcut_needs_recreate(startup_dir: Path, pythonw: Path) -> bool:
    """Read-only plan: does the app shortcut need (re)creating?"""
    info = autostart.inspect_shortcut(startup_dir)
    correct = (info is not None and Path(info.target) == pythonw
               and info.arguments == TRAY_ARGUMENTS)
    return not correct


def switch_startup_shortcut(startup_dir: Path, pythonw: Path) -> None:
    if shortcut_needs_recreate(startup_dir, pythonw):
        lnk = autostart.create_shortcut(
            startup_dir, pythonw, arguments=TRAY_ARGUMENTS)
        print(f"Created Startup shortcut: {lnk}")
    else:
        print("Startup shortcut already correct; skipping.")


def spawn_tray(pythonw: Path, repo_root: Path) -> None:
    """The Popen seam: detached tray launch, no console, survives exit."""
    subprocess.Popen(
        [str(pythonw), "-m", "socks_http_bridge", "--tray"],
        cwd=str(repo_root), close_fds=True,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)


def wait_for_listener(address: tuple[str, int], timeout: float = 10.0) -> bool:
    """The socket-probe seam: TCP-connect until it answers or timeout."""
    deadline = monotonic() + timeout
    while True:
        try:
            with socket.create_connection(address, timeout=1.0):
                return True
        except OSError:
            if monotonic() >= deadline:
                return False
            sleep(0.5)


def _load_config() -> Config:
    """The user's Config for the verify address/log path; defaults if
    unreadable (install must not die on a bad Config)."""
    try:
        return Config.load()
    except ConfigError:
        return Config()


def _print_plan(sync_cmd: list[str], tray: list[Proc],
                startup: Path, pythonw: Path,
                address: tuple[str, int]) -> None:
    print("dry-run: planning only, no changes will be made")
    print(f"  would run: {' '.join(sync_cmd)}")
    if shortcut_needs_recreate(startup, pythonw):
        print(f"  would create Startup shortcut {autostart.SHORTCUT_NAME} "
              f"-> {pythonw} {TRAY_ARGUMENTS}")
    else:
        print("  Startup shortcut already correct; would skip")
    if tray:
        print(f"  tray already running (PID {tray[0].pid}); would skip launch")
    else:
        print(f"  would launch: {pythonw} -m socks_http_bridge --tray")
    print(f"  would verify Listener {address[0]}:{address[1]} "
          "via TCP connect (up to 10 s)")


def install(repo_root: Path, *, startup_dir: Path | None = None,
            dry_run: bool = False) -> int:
    """Run the install; return the process exit code (0 = success)."""
    startup = _startup_dir() if startup_dir is None else startup_dir
    pythonw = repo_root / ".venv" / "Scripts" / "pythonw.exe"
    uv = find_uv()
    sync_cmd = [uv, "sync"]
    if KNOWN_PYTHON.is_file():
        sync_cmd += ["--python", str(KNOWN_PYTHON)]
    procs = enumerate_processes()
    tray = [p for p in procs if is_tray(p.commandline)]
    cfg = _load_config()
    # Self-connect target: a wildcard Listen must be dialed via loopback
    # (Windows rejects connect() to 0.0.0.0/:: with WinError 10049).
    address = connect_target(parse_address(cfg.listeners[0], "listeners"))
    if dry_run:
        _print_plan(sync_cmd, tray, startup, pythonw, address)
        return 0

    # 1. Sync first: a broken sync aborts before the shortcut is touched
    #    or the tray is launched (pinned by test_sync_runs_before_the_rest).
    print("Syncing dependencies (uv sync)...")
    result = run(sync_cmd, cwd=repo_root)
    if result.returncode != 0:
        raise InstallError(
            f"uv sync failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}")
    # 2. Create the Startup shortcut if it is missing or stale.
    switch_startup_shortcut(startup, pythonw)
    # 3. Launch the tray unless it is already up (mutex guards anyway).
    if tray:
        print(f"Tray already running (PID {tray[0].pid}); skipping launch "
              "(single-instance mutex).")
    else:
        print("Starting the tray Bridge...")
        spawn_tray(pythonw, repo_root)
    # 4. Verify the Listener answers.
    print(f"Verifying Listener {address[0]}:{address[1]} (up to 10 s)...")
    if wait_for_listener(address):
        if tray:
            print("Already installed: the tray Bridge is running and the "
                  "Listener responds.")
        else:
            print(f"OK: Listener {address[0]}:{address[1]} responds.")
        return 0
    print(f"FAILED: Listener {address[0]}:{address[1]} did not respond "
          f"within 10 s. Check the log: {os.path.expandvars(cfg.log_path)}")
    print("If the tray icon is grey/red, use Stop then Start in the tray "
          "menu (another process may hold the port).")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Install the tray Bridge: uv sync, Startup shortcut, "
                    "launch, verify (issue #9).")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned actions without side effects")
    args = ap.parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    try:
        return install(repo_root, dry_run=args.dry_run)
    except InstallError as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
