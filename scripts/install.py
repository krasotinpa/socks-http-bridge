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
only (no sync, writes or launch). A fresh install never stops a running
process.

Hosted mode (issue #48, spec #26): --source-tarball PATH installs or
updates a per-user app dir (--app-dir, default BRIDGE_INSTALL_DIR or
%LOCALAPPDATA%\\socks-http-bridge\\app) from a source tarball. Channels:
stable (--version TAG) or main (--sha SHA); a bare re-run honors the
channel recorded in install-marker.json. Same target with the tray up
-> no-op. Only an update stops the tray (graceful close attempt, then
force kill; waits for process exit and mutex release) and restarts it
after the source swap; the .venv is preserved.

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
import tarfile
import tempfile
from collections.abc import Callable
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


def wait_until(predicate: Callable[[], bool], timeout: float,
               interval: float = 0.2) -> bool:
    """Poll predicate until true or timeout (fake-clock seam)."""
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            return False
        sleep(interval)
    return True


def _listener_up(address: tuple[str, int]) -> bool:
    try:
        with socket.create_connection(address, timeout=1.0):
            return True
    except OSError:
        return False


def wait_for_listener(address: tuple[str, int], timeout: float = 10.0) -> bool:
    """The socket-probe seam: TCP-connect until it answers or timeout."""
    return wait_until(lambda: _listener_up(address), timeout, interval=0.5)


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



# --- Hosted install/update core (issue #48, spec #26) ---

MARKER_NAME = "install-marker.json"
RETRY_HINT = "Re-run the one-liner to retry."
# Keep in sync with tray.MUTEX_NAME (pinned by a drift test).
MUTEX_NAME = "Local\\SocksHttpBridgeTray"


def default_app_dir() -> Path:
    """BRIDGE_INSTALL_DIR env override, else the per-user app layout."""
    env = os.environ.get("BRIDGE_INSTALL_DIR")
    if env:
        return Path(env)
    return LOCAL_APP_DATA / "socks-http-bridge" / "app"


def is_app_tray(commandline: str, app_dir: Path) -> bool:
    """Strict matcher: only the tray launched from THIS app dir's venv."""
    pythonw = app_dir / ".venv" / "Scripts" / "pythonw.exe"
    return is_tray(commandline) and str(pythonw).lower() in commandline.lower()


def read_marker(app_dir: Path) -> dict | None:
    """The installed channel + tag/sha; missing or corrupt -> None."""
    try:
        data = json.loads((app_dir / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_marker(app_dir: Path, marker: dict) -> None:
    (app_dir / MARKER_NAME).write_text(
        json.dumps(marker, indent=2) + "\n", encoding="utf-8")


def plan_hosted(marker: dict | None, channel: str | None,
                version: str | None, sha: str | None,
                tray_running: bool) -> str:
    """fresh | update | no-op. Same target with the tray up -> no-op;
    an explicit channel switch always installs the resolved target."""
    if marker is None:
        # A running app-dir tray proves a prior install: self-heal as an
        # update (spec #26) instead of swapping a live venv as "fresh".
        return "update" if tray_running else "fresh"
    eff_channel = channel or marker.get("channel") or "stable"
    same = (marker.get("version") == version if eff_channel == "stable"
            else marker.get("sha") == sha)
    switched = (channel is not None and marker.get("channel") is not None
                and channel != marker.get("channel"))
    if same and tray_running and not switched:
        return "no-op"
    return "update"


WM_CLOSE = 0x0010
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000


def request_graceful_close(pid: int) -> None:
    """Best-effort WM_CLOSE to the tray's top-level windows (spike per
    spec #26: the force kill below is the reliable part)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnds: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(collect, 0)
    for hwnd in hwnds:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def process_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def mutex_held() -> bool:
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    name = os.environ.get("BRIDGE_MUTEX_NAME", MUTEX_NAME)
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, name)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def wait_process_exit(pid: int, timeout: float) -> bool:
    return wait_until(lambda: not process_alive(pid), timeout)


def wait_mutex_release(timeout: float) -> bool:
    return wait_until(lambda: not mutex_held(), timeout)


def stop_app_tray(tray: list[Proc]) -> None:
    """Graceful WM_CLOSE attempt, then force kill; wait for process exit
    AND mutex release before the caller swaps source (spec #26)."""
    if not tray:
        return
    pid = tray[0].pid
    print(f"Stopping the running tray (PID {pid}); updating drops active "
          "tunnels until the Bridge restarts.")
    request_graceful_close(pid)
    if not wait_process_exit(pid, 5.0):
        run(["taskkill", "/F", "/PID", str(pid)])
        if not wait_process_exit(pid, 10.0):
            raise InstallError(
                f"tray PID {pid} did not exit after force kill. {RETRY_HINT}")
    if not wait_mutex_release(10.0):
        raise InstallError(
            f"single-instance mutex still held after tray exit. {RETRY_HINT}")


def extract_source(tarball: Path, staging: Path) -> Path:
    """Fully extract the source tarball BEFORE any stop (spec #26).
    GitHub codeload tarballs carry one top-level directory; strip it."""
    raw = staging / "raw"
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            tf.extractall(raw, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise InstallError(
            f"cannot extract {tarball}: {exc}. {RETRY_HINT}") from exc
    entries = list(raw.iterdir()) if raw.is_dir() else []
    if len(entries) != 1 or not entries[0].is_dir():
        raise InstallError(
            f"unexpected tarball layout (want one top-level dir). {RETRY_HINT}")
    root = entries[0]
    if not (root / "pyproject.toml").is_file():
        raise InstallError(
            f"tarball has no pyproject.toml at its root. {RETRY_HINT}")
    return root


def swap_source(app_dir: Path, new_root: Path, staging: Path) -> None:
    """Fast same-volume renames: old source aside, new source in, the
    existing .venv preserved; no stale files survive (spec #26)."""
    venv = app_dir / ".venv"
    saved = staging / "venv-saved"
    old = staging / "old"
    try:
        if venv.is_dir():
            venv.rename(saved)
        if app_dir.exists():
            app_dir.rename(old)
        new_root.rename(app_dir)
    except OSError as exc:
        # Best-effort restore: the staging copies may be the only venv /
        # old source left, so put them back before reporting (spec #26).
        if not app_dir.exists() and old.exists():
            old.rename(app_dir)
        if saved.exists() and not (app_dir / ".venv").exists():
            saved.rename(app_dir / ".venv")
        raise InstallError(f"source swap failed: {exc}. {RETRY_HINT}") from exc
    if saved.exists():
        saved.rename(app_dir / ".venv")
    shutil.rmtree(old, ignore_errors=True)


def install_hosted(*, source_tarball: Path, app_dir: Path,
                   channel: str | None = None, version: str | None = None,
                   sha: str | None = None, startup_dir: Path | None = None,
                   dry_run: bool = False) -> int:
    """Hosted install/update from a source tarball (issue #48, spec #26).

    A fresh install never stops a running process; only an update
    restarts the tray. Same target with the tray up -> no-op.
    """
    marker = read_marker(app_dir)
    eff_channel = channel or (marker or {}).get("channel") or "stable"
    if eff_channel not in ("stable", "main"):
        raise InstallError(f"unknown channel {eff_channel!r} (stable|main)")
    if eff_channel == "stable" and not version:
        raise InstallError("stable channel needs --version <release tag>")
    if eff_channel == "main" and not sha:
        raise InstallError("main channel needs --sha <commit sha>")
    procs = enumerate_processes()
    app_tray = [p for p in procs if is_app_tray(p.commandline, app_dir)]
    others = [p for p in procs
              if is_tray(p.commandline)
              and not is_app_tray(p.commandline, app_dir)]
    mode = plan_hosted(marker, channel, version, sha, bool(app_tray))
    target = version if eff_channel == "stable" else sha
    assert target is not None  # validated above
    if mode == "no-op":
        print(f"Already up to date: channel {eff_channel}, target {target}; "
              f"the tray Bridge is running (PID {app_tray[0].pid}).")
        return 0
    startup = _startup_dir() if startup_dir is None else startup_dir
    pythonw = app_dir / ".venv" / "Scripts" / "pythonw.exe"
    try:
        uv = find_uv()
    except InstallError:
        if not dry_run:
            raise
        uv = None  # dry-run still prints a plan without uv
    sync_cmd = [uv or "uv", "sync"]
    if KNOWN_PYTHON.is_file():
        sync_cmd += ["--python", str(KNOWN_PYTHON)]
    cfg = _load_config()
    address = connect_target(parse_address(cfg.listeners[0], "listeners"))
    launch = not others if mode == "update" else not (app_tray or others)
    if dry_run:
        print("dry-run: planning only, no changes will be made")
        print(f"  mode: {mode}; channel {eff_channel}, target {target}")
        print(f"  app dir: {app_dir}")
        print(f"  uv: {uv or 'not found'}")
        print(f"  python: {KNOWN_PYTHON if KNOWN_PYTHON.is_file() else 'uv default'}")
        tray_state = (f"app tray PID {app_tray[0].pid}" if app_tray
                      else f"foreign tray PID {others[0].pid}" if others
                      else "not running")
        print(f"  tray: {tray_state}")
        print(f"  would extract {source_tarball} to a temp dir before any stop")
        if mode == "update" and app_tray:
            print(f"  would stop tray PID {app_tray[0].pid} (graceful close, "
                  "then force kill; active tunnels drop until restart)")
        print("  would swap source (preserving .venv), then run: "
              + " ".join(sync_cmd))
        print(f"  would write {MARKER_NAME} and point the Startup shortcut "
              f"at {pythonw}")
        if launch:
            print(f"  would launch: {pythonw} -m socks_http_bridge --tray")
        else:
            print("  another tray holds the single-instance mutex; "
                  "would skip launch")
        print(f"  would verify Listener {address[0]}:{address[1]} "
              "via TCP connect (up to 10 s)")
        return 0
    app_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="bridge-update-", dir=app_dir.parent))
    try:
        # 1. Extract fully BEFORE stopping anything (spec #26).
        print(f"Extracting {source_tarball}...")
        new_root = extract_source(source_tarball, staging)
        # 2. Update only: stop the app-dir tray; wait exit AND mutex.
        if mode == "update":
            stop_app_tray(app_tray)
        # 3. Swap source, preserving the virtual environment.
        swap_source(app_dir, new_root, staging)
        # 4. Sync: a broken sync aborts before marker/shortcut/launch.
        print("Syncing dependencies (uv sync)...")
        result = run(sync_cmd, cwd=app_dir)
        if result.returncode != 0:
            raise InstallError(
                f"uv sync failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()} {RETRY_HINT}")
        # 5. Record the installed target, then pin the Startup shortcut.
        marker_out = {"channel": eff_channel}
        marker_out["version" if eff_channel == "stable" else "sha"] = target
        write_marker(app_dir, marker_out)
        switch_startup_shortcut(startup, pythonw)
        # 6. Launch unless a foreign tray holds the single-instance mutex.
        if not launch:
            print("Another tray instance holds the single-instance mutex; "
                  "skipping launch.")
        else:
            print("Starting the tray Bridge...")
            spawn_tray(pythonw, app_dir)
        # 7. Verify the Listener answers.
        print(f"Verifying Listener {address[0]}:{address[1]} (up to 10 s)...")
        if wait_for_listener(address):
            print(f"OK: channel {eff_channel}, target {target}; "
                  f"Listener {address[0]}:{address[1]} responds.")
            return 0
        print(f"FAILED: Listener {address[0]}:{address[1]} did not respond "
              f"within 10 s. Check the log: {os.path.expandvars(cfg.log_path)}")
        print(RETRY_HINT)
        return 1
    finally:
        if (staging / "venv-saved").exists() or (staging / "old").exists():
            print(f"NOTE: staging kept at {staging} (holds the previous "
                  "install; manual recovery may be needed).")
        else:
            shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Install the tray Bridge: uv sync, Startup shortcut, "
                    "launch, verify (issue #9).")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan; run nothing, change nothing")
    ap.add_argument("--source-tarball", metavar="PATH",
                    help="hosted mode: install/update the app dir from a "
                         "source tarball (issue #48)")
    ap.add_argument("--app-dir", metavar="PATH",
                    help="hosted app dir (default: BRIDGE_INSTALL_DIR or "
                         "%%LOCALAPPDATA%%\\socks-http-bridge\\app)")
    ap.add_argument("--channel", choices=["stable", "main"],
                    help="hosted channel (default: persisted marker, else "
                         "stable)")
    ap.add_argument("--version", metavar="TAG",
                    help="stable channel target: release tag")
    ap.add_argument("--sha", metavar="SHA",
                    help="main channel target: commit sha")
    args = ap.parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    try:
        if args.source_tarball:
            app_dir = Path(args.app_dir) if args.app_dir else default_app_dir()
            return install_hosted(source_tarball=Path(args.source_tarball),
                                  app_dir=app_dir, channel=args.channel,
                                  version=args.version, sha=args.sha,
                                  dry_run=args.dry_run)
        return install(repo_root, dry_run=args.dry_run)
    except InstallError as exc:
        print(f"Install failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
