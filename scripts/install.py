"""Install + cutover from the old bridge.py prototype (issue #9).

Run from the repo root:  uv run python scripts/install.py [--dry-run]

Steps, in this order:
  1. uv sync -- broken dependencies must never kill a working bridge,
     so syncing happens strictly BEFORE the old bridge is stopped.
  2. Stop the old bridge: enumerate python/pythonw processes through
     PowerShell CIM and match the command line STRICTLY against
     tools\\socks-http-bridge\\bridge.py (separator-tolerant, case-
     insensitive, whole filename). Unrelated python processes are never
     touched. Stop is graceful first (taskkill without /F posts WM_CLOSE;
     a windowless pythonw usually refuses), then force (/F) if the
     process is still alive. If force fails, install aborts before
     touching the Startup shortcut or launching the tray.
  3. Switch the Startup shortcut: remove only .lnk files whose target or
     arguments point at the old bridge.py (identified by content, not by
     name), then create SocksHttpBridge.lnk through the autostart core
     (pythonw.exe -m socks_http_bridge --tray). Already correct -> skip.
  4. Launch the tray detached (DETACHED_PROCESS | CREATE_NO_WINDOW).
     A tray already running is left alone: the single-instance mutex
     would exit a second launch anyway.
  5. Verify: TCP-connect the first configured Listener (default
     127.0.0.1:8118) for up to ~10 s; on failure print the log path.

uv discovery: UV_BIN env var, then shutil.which("uv"), then the known
install-manager path on this laptop (KNOWN_UV below). uv sync gets
--python KNOWN_PYTHON when that shim exists (uv may not find the
install-manager Python on its own, see AGENTS.md).

Idempotent: no old process -> skip stop; shortcut already correct ->
skip; tray already running -> skip launch; Listener already answering
with the tray up -> "Already installed" report. --dry-run prints the
plan using read-only discovery only (no sync, stop, writes or launch).

Stdlib + the autostart core only. Reuses settings._startup_dir() and
settings.TRAY_ARGUMENTS (importing settings has no side effects).
"""

import argparse
import json
import os
import re
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

KNOWN_UV = Path(
    "C:/Users/krasotin/AppData/Local/Python/pythoncore-3.15-64/Scripts/uv.exe")
KNOWN_PYTHON = Path("C:/Users/krasotin/AppData/Local/Python/bin/python.exe")

# Whole filename: bridge.py.bak / bridge.pyw must NOT match.
OLD_BRIDGE = re.compile(
    r"tools[\\/]socks-http-bridge[\\/]bridge\.py(?![\w.])", re.IGNORECASE)

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


def find_uv() -> str:
    """UV_BIN env var -> PATH -> the known install-manager fallback."""
    env = os.environ.get("UV_BIN")
    if env:
        return env
    found = shutil.which("uv")
    if found:
        return found
    if KNOWN_UV.is_file():
        return str(KNOWN_UV)
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


def is_old_bridge(commandline: str) -> bool:
    return OLD_BRIDGE.search(commandline) is not None


def is_tray(commandline: str) -> bool:
    return "-m socks_http_bridge" in commandline and "--tray" in commandline


def _alive(pid: int) -> bool:
    return any(p.pid == pid for p in enumerate_processes())


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = monotonic() + timeout
    while _alive(pid):
        if monotonic() >= deadline:
            return False
        sleep(0.5)
    return True


def stop_old_bridge(targets: list[Proc]) -> None:
    """Graceful taskkill, then /F fallback; abort if the process lives."""
    if not targets:
        print("No old bridge process found; nothing to stop.")
        return
    for proc in targets:
        print(f"Stopping old bridge PID {proc.pid}: {proc.commandline}")
        # Graceful first: WM_CLOSE. A windowless pythonw usually refuses
        # (nonzero exit); that is expected, not an error.
        run(["taskkill", "/PID", str(proc.pid)])
        if _wait_gone(proc.pid, 5.0):
            continue
        result = run(["taskkill", "/F", "/PID", str(proc.pid)])
        if result.returncode != 0:
            raise InstallError(
                f"cannot stop old bridge PID {proc.pid}: "
                f"{result.stderr.strip()}")
        if not _wait_gone(proc.pid, 3.0):
            raise InstallError(
                f"old bridge PID {proc.pid} still alive after taskkill /F")
        print(f"Stopped old bridge PID {proc.pid} (forced).")


def read_lnk(lnk: Path) -> tuple[str, str]:
    """Read a shortcut's target and arguments via WScript.Shell."""
    quoted = str(lnk).replace("'", "''")
    result = run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
         f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{quoted}'); "
         "Write-Output $s.TargetPath; Write-Output $s.Arguments")])
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) < 2:
        raise InstallError(f"cannot read shortcut {lnk}: {result.stderr.strip()}")
    return lines[0], lines[1]


def shortcut_actions(startup_dir: Path,
                     pythonw: Path) -> tuple[list[Path], bool]:
    """Read-only plan: (old .lnk files to remove, app shortcut needs work).
    Old shortcuts are identified by target/arguments, never by name."""
    remove = []
    for lnk in sorted(startup_dir.glob("*.lnk")):
        target, arguments = read_lnk(lnk)
        if is_old_bridge(target) or is_old_bridge(arguments):
            remove.append(lnk)
    info = autostart.inspect_shortcut(startup_dir)
    correct = (info is not None and Path(info.target) == pythonw
               and info.arguments == TRAY_ARGUMENTS)
    return remove, not correct


def switch_startup_shortcut(startup_dir: Path, pythonw: Path) -> None:
    remove, needs_create = shortcut_actions(startup_dir, pythonw)
    for lnk in remove:
        print(f"Removing old Startup shortcut: {lnk.name}")
        lnk.unlink()
    if needs_create:
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


def _print_plan(sync_cmd: list[str], old: list[Proc], tray: list[Proc],
                startup: Path, pythonw: Path,
                address: tuple[str, int]) -> None:
    print("dry-run: planning only, no changes will be made")
    print(f"  would run: {' '.join(sync_cmd)}")
    for proc in old:
        print(f"  would stop old bridge PID {proc.pid}: {proc.commandline}")
    if not old:
        print("  no old bridge process found; nothing to stop")
    remove, needs_create = shortcut_actions(startup, pythonw)
    for lnk in remove:
        print(f"  would remove old Startup shortcut: {lnk.name}")
    if needs_create:
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
    old = [p for p in procs if is_old_bridge(p.commandline)]
    tray = [p for p in procs if is_tray(p.commandline)]
    cfg = _load_config()
    # Self-connect target: a wildcard Listen must be dialed via loopback
    # (Windows rejects connect() to 0.0.0.0/:: with WinError 10049).
    address = connect_target(parse_address(cfg.listeners[0], "listeners"))
    if dry_run:
        _print_plan(sync_cmd, old, tray, startup, pythonw, address)
        return 0

    # 1. Sync BEFORE stopping anything: broken deps must not kill the
    #    working bridge (pinned by test_sync_runs_before_stop_and_the_rest).
    print("Syncing dependencies (uv sync)...")
    result = run(sync_cmd, cwd=repo_root)
    if result.returncode != 0:
        raise InstallError(
            f"uv sync failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}")
    # 2. Stop the old bridge (strict matching inside stop_old_bridge's
    #    caller-side filter; graceful then force).
    stop_old_bridge(old)
    # 3. Switch the Startup shortcut.
    switch_startup_shortcut(startup, pythonw)
    # 4. Launch the tray unless it is already up (mutex guards anyway).
    if tray:
        print(f"Tray already running (PID {tray[0].pid}); skipping launch "
              "(single-instance mutex).")
    else:
        print("Starting the tray Bridge...")
        spawn_tray(pythonw, repo_root)
    # 5. Verify the Listener answers.
    print(f"Verifying Listener {address[0]}:{address[1]} (up to 10 s)...")
    if wait_for_listener(address):
        if tray and not old:
            print("Already installed: the tray Bridge is running and the "
                  "Listener responds.")
        else:
            print(f"OK: Listener {address[0]}:{address[1]} responds.")
        return 0
    print(f"FAILED: Listener {address[0]}:{address[1]} did not respond "
          f"within 10 s. Check the log: {os.path.expandvars(cfg.log_path)}")
    print("If the tray icon is grey/red, use Stop then Start in the tray "
          "menu (the old bridge may have held the port).")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Install the tray Bridge and cut over from the old "
                    "tools/socks-http-bridge/bridge.py (issue #9).")
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
