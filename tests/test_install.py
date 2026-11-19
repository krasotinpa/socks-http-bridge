"""Tests for scripts/install.py (issue #9 install).

Headless: every seam is fake -- the subprocess runner, process
enumeration, the shortcut switch, the tray Popen and the socket probe.
Shortcut behaviour is additionally exercised against real .lnk files in
pytest tmp_path directories (same convention as test_autostart.py); the
real Startup folder, the live Bridge, the live Upstream and the user's
Config are never touched.
"""

import importlib.util
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from socks_http_bridge import autostart
from socks_http_bridge.config import Config
from socks_http_bridge.settings import TRAY_ARGUMENTS

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "install_script", REPO_ROOT / "scripts" / "install.py")
assert _spec is not None and _spec.loader is not None
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)

PYTHONW = Path(sys.executable).with_name("pythonw.exe")


def _cp(cmd: list[str], rc: int = 0,
        stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, rc, stdout, stderr)


def make_uv(local: Path, version: str) -> Path:
    """Fake install-manager uv at <local>/Python/pythoncore-<version>-64."""
    uv = local / "Python" / f"pythoncore-{version}-64" / "Scripts" / "uv.exe"
    uv.parent.mkdir(parents=True, exist_ok=True)
    uv.touch()
    return uv


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fully fake install environment: repo, Startup dir, seams, clock."""
    repo = tmp_path / "repo"
    pythonw = repo / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.touch()
    startup = tmp_path / "startup"
    startup.mkdir()
    local = tmp_path / "local"
    uv = make_uv(local, "3.15")
    shim = tmp_path / "python.exe"
    shim.touch()

    r = SimpleNamespace(
        repo=repo, pythonw=pythonw, startup=startup, local=local, uv=uv,
        shim=shim, events=[], procs=[], sync_rc=0,
        verify_result=True, t=0.0,
    )

    def fake_run(cmd: list[str], *, cwd: Path | None = None) -> (
            subprocess.CompletedProcess[str]):
        r.events.append(("run", tuple(cmd), cwd))
        if len(cmd) > 1 and cmd[1] == "sync":
            return _cp(cmd, r.sync_rc, stderr="sync boom" if r.sync_rc else "")
        return _cp(cmd, 0)

    def fake_switch(startup_dir: Path, pythonw: Path) -> None:
        r.events.append(("switch", startup_dir, pythonw))

    def fake_spawn(pythonw: Path, repo_root: Path) -> None:
        r.events.append(("spawn", pythonw, repo_root))

    def fake_wait(address: tuple[str, int], timeout: float = 10.0) -> bool:
        r.events.append(("verify", address, timeout))
        return r.verify_result

    monkeypatch.setattr(install, "run", fake_run)
    monkeypatch.setattr(install, "enumerate_processes", lambda: list(r.procs))
    monkeypatch.setattr(install, "switch_startup_shortcut", fake_switch)
    monkeypatch.setattr(install, "spawn_tray", fake_spawn)
    monkeypatch.setattr(install, "wait_for_listener", fake_wait)
    monkeypatch.setattr(install, "_load_config", Config)
    monkeypatch.setattr(install, "monotonic", lambda: r.t)
    monkeypatch.setattr(install, "sleep", lambda s: setattr(r, "t", r.t + s))
    monkeypatch.setattr(install, "LOCAL_APP_DATA", local)
    monkeypatch.setattr(install, "KNOWN_PYTHON", shim)
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    monkeypatch.delenv("UV_BIN", raising=False)
    return r


def _cmds(r: SimpleNamespace) -> list[tuple[str, ...]]:
    return [e[1] for e in r.events if e[0] == "run"]


def test_sync_runs_before_the_rest(rig: SimpleNamespace) -> None:
    assert install.install(rig.repo, startup_dir=rig.startup) == 0
    kinds = [e[0] for e in rig.events]
    cmds = _cmds(rig)
    assert cmds[0][0] == str(rig.uv) and cmds[0][1] == "sync"
    assert kinds.index("switch") > kinds.index("run")
    assert kinds.index("spawn") > kinds.index("switch")
    assert kinds.index("verify") > kinds.index("spawn")


def test_uv_sync_failure_aborts_install(rig: SimpleNamespace) -> None:
    rig.sync_rc = 1
    with pytest.raises(install.InstallError):
        install.install(rig.repo, startup_dir=rig.startup)
    assert not any(e[0] in ("switch", "spawn", "verify") for e in rig.events)


def test_install_never_stops_processes(rig: SimpleNamespace) -> None:
    # No cutover: even a prototype-shaped command line is left alone.
    rig.procs = [
        install.Proc(200, "python.exe -m hermes_cli.main gateway run"),
        install.Proc(201, "python.exe C:\\other\\project\\bridge.py"),
        install.Proc(202, "python.exe C:\\tools\\socks-http-bridge\\bridge.py"),
    ]
    assert install.install(rig.repo, startup_dir=rig.startup) == 0
    assert not any(c[0] == "taskkill" for c in _cmds(rig))
    assert any(e[0] == "spawn" for e in rig.events)


def test_idempotent_rerun_with_tray_already_running(
        rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    rig.procs = [install.Proc(300, f'"{PYTHONW}" -m socks_http_bridge --tray')]
    for _ in range(2):
        assert install.install(rig.repo, startup_dir=rig.startup) == 0
    assert not any(e[0] == "spawn" for e in rig.events)  # mutex: no second tray
    assert "Already installed" in capsys.readouterr().out


def test_dry_run_has_no_side_effects(
        rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    rig.procs = [install.Proc(300, f'"{PYTHONW}" -m socks_http_bridge --tray')]
    assert install.install(rig.repo, startup_dir=rig.startup,
                           dry_run=True) == 0
    for cmd in _cmds(rig):
        assert cmd[0] != "taskkill"
        assert "sync" not in cmd
    assert not any(e[0] in ("switch", "spawn", "verify") for e in rig.events)
    assert list(rig.startup.iterdir()) == []
    out = capsys.readouterr().out
    assert "dry-run" in out and "300" in out  # the plan names the running tray


def test_dry_run_plan_reports_pending_work(
        rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    assert install.install(rig.repo, startup_dir=rig.startup,
                           dry_run=True) == 0
    out = capsys.readouterr().out
    assert "would create" in out and "would launch" in out


def test_verify_failure_reports_log_path(
        rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    rig.verify_result = False
    assert install.install(rig.repo, startup_dir=rig.startup) == 1
    out = capsys.readouterr().out
    assert "bridge.log" in out and "8118" in out


def test_verify_wildcard_listener_connects_via_loopback(
        rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    # connect() to a wildcard bind address fails on Windows (WinError
    # 10049); verify must dial the loopback equivalent instead.
    monkeypatch.setattr(install, "_load_config",
                        lambda: Config(listeners=["0.0.0.0:18118"]))
    assert install.install(rig.repo, startup_dir=rig.startup) == 0
    verify = [e for e in rig.events if e[0] == "verify"]
    assert verify == [("verify", ("127.0.0.1", 18118), 10.0)]


# --- Shortcut: real .lnk files in tmp_path, real autostart core ---

def make_lnk(lnk: Path, target: str, arguments: str) -> None:
    """Write a shortcut with an arbitrary name via WScript.Shell."""
    quoted = str(lnk).replace("'", "''")
    target_q = target.replace("'", "''")
    arguments_q = arguments.replace("'", "''")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
          f"'{quoted}'); $s.TargetPath = '{target_q}'; "
          f"$s.Arguments = '{arguments_q}'; $s.Save()")],
        check=True, capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW)


def test_switch_creates_app_shortcut_and_leaves_others_alone(
        tmp_path: Path) -> None:
    startup = tmp_path / "startup"
    startup.mkdir()
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    make_lnk(startup / "OtherBridge.lnk", str(PYTHONW),
             "C:\\tools\\socks-http-bridge\\bridge.py")
    make_lnk(startup / "Telegram.lnk", "C:\\Telegram\\Telegram.exe",
             "-autostart")
    install.switch_startup_shortcut(startup, pythonw)
    assert (startup / "OtherBridge.lnk").is_file()  # no cutover: untouched
    assert (startup / "Telegram.lnk").is_file()  # unrelated .lnk untouched
    info = autostart.inspect_shortcut(startup)
    assert info is not None
    assert info.target == str(pythonw)
    assert info.arguments == TRAY_ARGUMENTS


def test_switch_idempotent_when_shortcut_already_correct(
        tmp_path: Path) -> None:
    startup = tmp_path / "startup"
    startup.mkdir()
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    lnk = autostart.create_shortcut(startup, pythonw, arguments=TRAY_ARGUMENTS)
    before = lnk.stat().st_mtime_ns
    install.switch_startup_shortcut(startup, pythonw)
    assert lnk.stat().st_mtime_ns == before  # not rewritten
    info = autostart.inspect_shortcut(startup)
    assert info is not None and info.arguments == TRAY_ARGUMENTS


def test_switch_overwrites_stale_app_shortcut(tmp_path: Path) -> None:
    startup = tmp_path / "startup"
    startup.mkdir()
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    autostart.create_shortcut(startup, pythonw)  # default args, no --tray
    install.switch_startup_shortcut(startup, pythonw)
    info = autostart.inspect_shortcut(startup)
    assert info is not None
    assert info.target == str(pythonw)
    assert info.arguments == TRAY_ARGUMENTS


# --- Popen, socket probe and uv discovery seams ---

def test_spawn_tray_command_and_flags(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> None:
        captured["cmd"] = cmd
        captured.update(kwargs)

    monkeypatch.setattr(install.subprocess, "Popen", fake_popen)
    pythonw = tmp_path / "pythonw.exe"
    install.spawn_tray(pythonw, tmp_path)
    assert captured["cmd"] == [str(pythonw), "-m", "socks_http_bridge", "--tray"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["creationflags"] == (
        subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)


def test_wait_for_listener_on_ephemeral_port() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    address = srv.getsockname()
    try:
        assert install.wait_for_listener(address, timeout=2.0) is True
    finally:
        srv.close()
    assert install.wait_for_listener(address, timeout=0.3) is False


def test_find_uv_precedence(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = tmp_path / "local"
    uv = make_uv(local, "3.15")
    monkeypatch.setattr(install, "LOCAL_APP_DATA", local)
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    monkeypatch.setenv("UV_BIN", "X:/custom/uv.exe")
    assert install.find_uv() == "X:/custom/uv.exe"
    monkeypatch.delenv("UV_BIN")
    monkeypatch.setattr(install.shutil, "which",
                        lambda _name: "C:/path/uv.exe")
    assert install.find_uv() == "C:/path/uv.exe"
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    assert install.find_uv() == str(uv)
    monkeypatch.setattr(install, "LOCAL_APP_DATA", tmp_path / "missing")
    with pytest.raises(install.InstallError):
        install.find_uv()


def test_find_uv_picks_newest_install_manager_version(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = tmp_path / "local"
    # 3.9 sorts AFTER 3.15 lexicographically: the order must be numeric.
    for version in ("3.9", "3.13", "3.15"):
        make_uv(local, version)
    monkeypatch.setattr(install, "LOCAL_APP_DATA", local)
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    monkeypatch.delenv("UV_BIN", raising=False)
    assert install.find_uv() == str(make_uv(local, "3.15"))
