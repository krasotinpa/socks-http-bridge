"""Tests for scripts/install.py (issue #9 install).

Headless: every seam is fake -- the subprocess runner, process
enumeration, the shortcut switch, the tray Popen and the socket probe.
Shortcut behaviour is additionally exercised against real .lnk files in
pytest tmp_path directories (same convention as test_autostart.py); the
real Startup folder, the live Bridge, the live Upstream and the user's
Config are never touched.
"""

import importlib.util
import io
import json
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


# --- Hosted install/update core (issue #48, spec #26) ---

@pytest.fixture
def hosted(rig: SimpleNamespace, tmp_path: Path,
           monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The rig plus a hosted app dir and fakes for the stop/extract seams.

    graceful_success: WM_CLOSE makes the process exit; kill_works: taskkill
    makes it exit; mutex_releases: the mutex frees after exit.
    """
    rig.app = tmp_path / "appdata" / "app"
    rig.graceful_success = False
    rig.kill_works = True
    rig.mutex_releases = True

    def fake_graceful(pid: int) -> None:
        rig.events.append(("graceful", pid))

    def fake_alive(pid: int) -> bool:
        if rig.graceful_success:
            return False
        killed = any(e[0] == "run" and e[1][0] == "taskkill"
                     and str(pid) in e[1] for e in rig.events)
        return not (killed and rig.kill_works)

    def fake_mutex() -> bool:
        return not rig.mutex_releases

    monkeypatch.setattr(install, "request_graceful_close", fake_graceful)
    monkeypatch.setattr(install, "process_alive", fake_alive)
    monkeypatch.setattr(install, "mutex_held", fake_mutex)
    real_extract = install.extract_source

    def fake_extract(tarball: Path, staging: Path) -> Path:
        rig.events.append(("extract", tarball, staging))
        return real_extract(tarball, staging)

    monkeypatch.setattr(install, "extract_source", fake_extract)
    return rig


def app_tray_proc(r: SimpleNamespace, pid: int = 400) -> install.Proc:
    pythonw = r.app / ".venv" / "Scripts" / "pythonw.exe"
    return install.Proc(pid, f'"{pythonw}" -m socks_http_bridge --tray')


def test_hosted_noop_when_up_to_date_and_tray_running(
        hosted: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.1.0"}), encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]
    rc = install.install_hosted(
        source_tarball=Path("X:/nonexistent.tar.gz"), app_dir=app,
        channel="stable", version="0.1.0", startup_dir=hosted.startup)
    assert rc == 0
    assert hosted.events == []  # no sync, stop, swap, spawn or verify
    assert "up to date" in capsys.readouterr().out.lower()


def make_tarball(path: Path, files: dict[str, bytes],
                 top: str = "socks-http-bridge-0.1.0") -> Path:
    """A GitHub-codeload-shaped source tarball: one top-level dir."""
    import tarfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


NEW_SOURCE = {
    "pyproject.toml": b'[project]\nname = "socks-http-bridge"\n',
    "src/socks_http_bridge/__init__.py": b"# new source\n",
}


def test_hosted_fresh_install_end_to_end(
        hosted: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    app = hosted.app
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    kinds = [e[0] for e in hosted.events]
    assert kinds == ["extract", "run", "switch", "spawn", "verify"]
    sync = next(e for e in hosted.events if e[0] == "run")
    assert sync[1][1] == "sync" and sync[2] == app  # sync runs in the app dir
    assert (app / "pyproject.toml").is_file()
    assert (app / "src" / "socks_http_bridge" / "__init__.py").is_file()
    marker = json.loads((app / "install-marker.json").read_text(encoding="utf-8"))
    assert marker == {"channel": "stable", "version": "0.1.0"}
    assert list(app.parent.iterdir()) == [app]  # staging cleaned up
    assert "0.1.0" in capsys.readouterr().out


def test_hosted_update_stops_tray_then_swaps_preserving_venv(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    venv = app / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "pythonw.exe").write_bytes(b"venv")
    (venv / "keep.txt").write_text("venv intact", encoding="utf-8")
    (app / "stale.txt").write_text("old", encoding="utf-8")
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.0.9"}), encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]
    hosted.graceful_success = False  # WM_CLOSE ineffective -> force kill
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    kinds = [e[0] for e in hosted.events]
    # extract fully BEFORE stop; kill BEFORE sync; then switch/spawn/verify
    assert kinds == ["extract", "graceful", "run", "run",
                     "switch", "spawn", "verify"]
    runs = [e for e in hosted.events if e[0] == "run"]
    assert runs[0][1][0] == "taskkill" and "400" in runs[0][1]
    assert runs[1][1][1] == "sync"
    assert (venv / "keep.txt").read_text(encoding="utf-8") == "venv intact"
    assert (app / "pyproject.toml").is_file()
    assert not (app / "stale.txt").exists()  # no stale files survive
    marker = json.loads((app / "install-marker.json").read_text(encoding="utf-8"))
    assert marker == {"channel": "stable", "version": "0.1.0"}
    assert list(app.parent.iterdir()) == [app]


def test_hosted_update_graceful_close_avoids_force_kill(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.0.9"}), encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]
    hosted.graceful_success = True
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    assert ("graceful", 400) in hosted.events
    assert not any(e[0] == "run" and e[1][0] == "taskkill"
                   for e in hosted.events)


def test_hosted_fresh_never_stops_a_running_tray(
        hosted: SimpleNamespace) -> None:
    # A foreign tray (other checkout) holds the mutex: fresh install skips
    # the launch but never stops it. An app-dir tray without a marker is a
    # prior install and self-heals via the update path (separate test).
    app = hosted.app  # no marker, no app-dir tray -> fresh install
    hosted.procs = [install.Proc(
        pid=500, commandline='"C:\\other\\pythonw.exe" -m socks_http_bridge --tray')]
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    kinds = [e[0] for e in hosted.events]
    assert "graceful" not in kinds
    assert not any(e[0] == "run" and e[1][0] == "taskkill"
                   for e in hosted.events)
    assert "spawn" not in kinds  # the foreign tray keeps the mutex
    assert (app / "pyproject.toml").is_file()


def test_hosted_update_leaves_foreign_tray_untouched(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.0.9"}), encoding="utf-8")
    foreign = install.Proc(
        700, f'"{hosted.pythonw}" -m socks_http_bridge --tray')
    hosted.procs = [app_tray_proc(hosted), foreign]
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    assert ("graceful", 400) in hosted.events
    assert ("graceful", 700) not in hosted.events
    assert not any(e[0] == "run" and "700" in e[1] for e in hosted.events)
    assert "spawn" not in [e[0] for e in hosted.events]


def test_hosted_update_force_kill_failure_aborts_with_retry_hint(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "stale.txt").write_text("old", encoding="utf-8")
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.0.9"}), encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]
    hosted.kill_works = False
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    with pytest.raises(install.InstallError,
                       match=r"did not exit.*Re-run the one-liner"):
        install.install_hosted(source_tarball=tarball, app_dir=app,
                               channel="stable", version="0.1.0",
                               startup_dir=hosted.startup)
    kinds = [e[0] for e in hosted.events]
    assert kinds == ["extract", "graceful", "run"]  # aborted before sync/swap
    assert (app / "stale.txt").exists()  # nothing was swapped


def test_hosted_update_mutex_stuck_aborts_with_retry_hint(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.0.9"}), encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]
    hosted.mutex_releases = False
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    with pytest.raises(install.InstallError,
                       match=r"mutex still held.*Re-run the one-liner"):
        install.install_hosted(source_tarball=tarball, app_dir=app,
                               channel="stable", version="0.1.0",
                               startup_dir=hosted.startup)
    kinds = [e[0] for e in hosted.events]
    assert kinds == ["extract", "graceful", "run"]


def test_hosted_dry_run_plans_without_mutating(
        hosted: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / ".venv" / "Scripts").mkdir(parents=True)
    (app / "stale.txt").write_text("old", encoding="utf-8")
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.0.9"}), encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]
    before = {p: p.read_bytes() for p in app.rglob("*") if p.is_file()}
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup, dry_run=True)
    assert rc == 0
    assert hosted.events == []  # no stop/sync/switch/spawn/verify
    after = {p: p.read_bytes() for p in app.rglob("*") if p.is_file()}
    assert after == before  # nothing touched, marker intact
    assert list(app.parent.iterdir()) == [app]  # no staging left behind
    out = capsys.readouterr().out.lower()
    assert "dry-run" in out and "would" in out
    assert "0.1.0" in out and "stop" in out


def test_hosted_sync_failure_aborts_before_marker_and_launch(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    hosted.sync_rc = 1
    hosted.sync_err = "boom"
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    with pytest.raises(install.InstallError,
                       match=r"uv sync failed.*boom.*Re-run the one-liner"):
        install.install_hosted(source_tarball=tarball, app_dir=app,
                               channel="stable", version="0.1.0",
                               startup_dir=hosted.startup)
    assert (app / "pyproject.toml").is_file()  # swap happened
    assert not (app / "install-marker.json").exists()  # marker did not
    kinds = [e[0] for e in hosted.events]
    assert kinds == ["extract", "run"]  # no switch/spawn/verify


def test_hosted_bare_rerun_honors_persisted_main_channel(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "main", "sha": "abc123"}), encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]
    rc = install.install_hosted(source_tarball=Path("X:/nonexistent.tar.gz"),
                                app_dir=app, sha="abc123",
                                startup_dir=hosted.startup)
    assert rc == 0
    assert hosted.events == []  # no-op on the persisted main channel


def test_hosted_explicit_channel_switch_forces_install(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "main", "sha": "abc123"}), encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    assert ("graceful", 400) in hosted.events  # update path ran
    marker = json.loads((app / "install-marker.json").read_text(encoding="utf-8"))
    assert marker == {"channel": "stable", "version": "0.1.0"}


def test_hosted_same_version_without_tray_reinstalls_and_launches(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.1.0"}), encoding="utf-8")
    hosted.procs = []  # tray not running
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    kinds = [e[0] for e in hosted.events]
    assert "graceful" not in kinds  # nothing to stop
    assert "spawn" in kinds  # tray relaunched


def test_cli_dispatches_hosted_mode(monkeypatch: pytest.MonkeyPatch,
                                    tmp_path: Path) -> None:
    seen: dict = {}

    def fake_hosted(**kw: object) -> int:
        seen.update(kw)
        return 0

    monkeypatch.setattr(install, "install_hosted", fake_hosted)
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--source-tarball", "X:/s.tar.gz",
        "--app-dir", str(tmp_path / "a"), "--channel", "main",
        "--sha", "abc123", "--dry-run"])
    assert install.main() == 0
    assert seen["source_tarball"] == Path("X:/s.tar.gz")
    assert seen["app_dir"] == tmp_path / "a"
    assert seen["channel"] == "main" and seen["sha"] == "abc123"
    assert seen["dry_run"] is True


def test_cli_hosted_app_dir_defaults_to_env(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    monkeypatch.setenv("BRIDGE_INSTALL_DIR", str(tmp_path / "envapp"))
    seen: dict = {}

    def fake_hosted(**kw: object) -> int:
        seen.update(kw)
        return 0

    monkeypatch.setattr(install, "install_hosted", fake_hosted)
    monkeypatch.setattr(sys, "argv", [
        "install.py", "--source-tarball", "X:/s.tar.gz", "--version", "0.1.0"])
    assert install.main() == 0
    assert seen["app_dir"] == tmp_path / "envapp"
    assert seen["channel"] is None and seen["version"] == "0.1.0"


def test_default_app_dir_layout(monkeypatch: pytest.MonkeyPatch,
                                tmp_path: Path) -> None:
    monkeypatch.delenv("BRIDGE_INSTALL_DIR", raising=False)
    monkeypatch.setattr(install, "LOCAL_APP_DATA", tmp_path)
    assert install.default_app_dir() == tmp_path / "socks-http-bridge" / "app"


def test_extract_source_strips_top_dir(tmp_path: Path) -> None:
    good = make_tarball(tmp_path / "good.tar.gz", NEW_SOURCE)
    root = install.extract_source(good, tmp_path / "st")
    assert root.name == "socks-http-bridge-0.1.0"
    assert (root / "pyproject.toml").is_file()


def test_extract_source_requires_pyproject(tmp_path: Path) -> None:
    bad = make_tarball(tmp_path / "bad.tar.gz", {"readme.txt": b"x"})
    with pytest.raises(install.InstallError, match="pyproject.toml"):
        install.extract_source(bad, tmp_path / "st2")


def test_extract_source_rejects_path_traversal(tmp_path: Path) -> None:
    import tarfile
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tf:
        info = tarfile.TarInfo("socks-http-bridge-0.1.0/../../pwned.txt")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(install.InstallError, match="cannot extract"):
        install.extract_source(evil, tmp_path / "st3")
    assert not (tmp_path / "pwned.txt").exists()


def test_is_app_tray_strict_matching(tmp_path: Path) -> None:
    app = tmp_path / "app"
    pw = str(app / ".venv" / "Scripts" / "pythonw.exe")
    assert install.is_app_tray(f'"{pw}" -m socks_http_bridge --tray', app)
    # path matching is case-insensitive; module args follow is_tray
    assert install.is_app_tray(
        f'"{pw.upper()}" -m socks_http_bridge --tray', app)
    other = tmp_path / "other"
    opw = str(other / ".venv" / "Scripts" / "pythonw.exe")
    assert not install.is_app_tray(f'"{opw}" -m socks_http_bridge --tray', app)
    assert not install.is_app_tray(f'"{pw}" -m socks_http_bridge', app)
    assert not install.is_app_tray(f'"{pw}"', app)


def test_hosted_mutex_name_matches_tray() -> None:
    from socks_http_bridge import tray
    assert install.MUTEX_NAME == tray.MUTEX_NAME


def test_hosted_corrupt_marker_with_running_tray_self_heals(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text("{not json", encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]  # proves a prior install
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    assert ("graceful", 400) in hosted.events  # update path, tray stopped
    marker = json.loads((app / "install-marker.json").read_text(encoding="utf-8"))
    assert marker == {"channel": "stable", "version": "0.1.0"}


def test_hosted_corrupt_marker_without_tray_is_fresh(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text("{not json", encoding="utf-8")
    hosted.procs = []
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    kinds = [e[0] for e in hosted.events]
    assert "graceful" not in kinds  # fresh install never stops anything
    assert "spawn" in kinds


def test_hosted_verify_failure_reports_and_hints(
        hosted: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    hosted.verify_result = False
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=hosted.app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "Re-run the one-liner" in out


def test_hosted_swap_failure_restores_venv_and_old_source(
        hosted: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    venv = app / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "keep.txt").write_text("venv intact", encoding="utf-8")
    (app / "stale.txt").write_text("old", encoding="utf-8")
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.0.9"}), encoding="utf-8")
    hosted.procs = []  # nothing to stop; isolate the swap window
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    real_rename = Path.rename

    def flaky(self: Path, target: Path) -> Path:
        if self.name == "socks-http-bridge-0.1.0" and Path(target) == app:
            raise OSError("locked")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(install.InstallError, match="swap failed"):
        install.install_hosted(source_tarball=tarball, app_dir=app,
                               channel="stable", version="0.1.0",
                               startup_dir=hosted.startup)
    assert (venv / "keep.txt").read_text(encoding="utf-8") == "venv intact"
    assert (app / "stale.txt").exists()  # old source restored
    marker = json.loads((app / "install-marker.json").read_text(encoding="utf-8"))
    assert marker["version"] == "0.0.9"  # marker untouched
    assert list(app.parent.iterdir()) == [app]  # staging cleaned after rollback


def test_hosted_noop_with_channelless_marker_and_explicit_stable(
        hosted: SimpleNamespace) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text(
        json.dumps({"version": "0.1.0"}), encoding="utf-8")  # no channel key
    hosted.procs = [app_tray_proc(hosted)]
    rc = install.install_hosted(source_tarball=Path("X:/nonexistent.tar.gz"),
                                app_dir=app, channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    assert hosted.events == []  # no-op, no forced restart


def test_hosted_update_warns_about_tunnel_drop(
        hosted: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.0.9"}), encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]
    tarball = make_tarball(hosted.repo.parent / "src.tar.gz", NEW_SOURCE)
    rc = install.install_hosted(source_tarball=tarball, app_dir=app,
                                channel="stable", version="0.1.0",
                                startup_dir=hosted.startup)
    assert rc == 0
    assert "tunnel" in capsys.readouterr().out.lower()


def test_hosted_dry_run_survives_missing_uv_and_reports_state(
        hosted: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    def no_uv() -> str:
        raise install.InstallError("uv not found")

    monkeypatch.setattr(install, "find_uv", no_uv)
    app = hosted.app
    app.mkdir(parents=True)
    (app / "install-marker.json").write_text(
        json.dumps({"channel": "stable", "version": "0.0.9"}), encoding="utf-8")
    hosted.procs = [app_tray_proc(hosted)]
    rc = install.install_hosted(source_tarball=Path("X:/nonexistent.tar.gz"),
                                app_dir=app, channel="stable", version="0.1.0",
                                startup_dir=hosted.startup, dry_run=True)
    assert rc == 0
    assert hosted.events == []
    out = capsys.readouterr().out.lower()
    assert "uv: not found" in out
    assert "python:" in out and "tray:" in out
