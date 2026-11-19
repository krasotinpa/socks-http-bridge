"""Tests for scripts/install.ps1 (issue #49, spec #26).

The bootstrapper is PowerShell, so its seam is the CLI boundary: real
powershell.exe runs against a source tarball built from this checkout,
with BRIDGE_INSTALL_DIR pointed at a tmp app dir. Only the -DryRun path
is exercised here (read-only by contract, pinned by test_install.py);
the networked install/update E2E is the CI smoke job (issue #50).
UV_PYTHON pins the pytest interpreter so uv never downloads a Python
during tests.
"""

import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "install.ps1"


def make_source_tarball(path: Path) -> Path:
    """A codeload-shaped tarball of the files delegation needs."""
    names = ["pyproject.toml", "scripts/install.py"]
    names += [p.relative_to(REPO_ROOT).as_posix()
              for p in (REPO_ROOT / "src" / "socks_http_bridge").glob("*.py")]
    with tarfile.open(path, "w:gz") as tf:
        for name in names:
            data = (REPO_ROOT / name).read_bytes()
            info = tarfile.TarInfo(f"socks-http-bridge-local/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def ps_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("BRIDGE_CHANNEL", "BRIDGE_SOURCE_TARBALL", "BRIDGE_DRYRUN",
                "BRIDGE_INSTALL_DIR", "UV_BIN"):
        env.pop(key, None)
    env["BRIDGE_INSTALL_DIR"] = str(tmp_path / "app")
    env["UV_PYTHON"] = sys.executable
    env.update(extra)
    return env


def write_marker(app: Path, channel: str = "main", sha: str = "abc123") -> None:
    app.mkdir()
    (app / "install-marker.json").write_text(
        f'{{"channel": "{channel}", "sha": "{sha}"}}', encoding="utf-8")


def run_ps(args: list[str], env: dict[str, str],
           command: str | None = None) -> subprocess.CompletedProcess[str]:
    invocation = (["-Command", command] if command is not None
                  else ["-File", str(SCRIPT), *args])
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", *invocation],
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)


def test_dry_run_source_override_plans_without_changes(tmp_path: Path) -> None:
    tarball = make_source_tarball(tmp_path / "source.tar.gz")
    result = run_ps(["-DryRun", "-SourceTarball", str(tarball)],
                    ps_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout.lower()
    assert "dry-run" in out
    assert "channel stable" in out
    assert "would extract" in out  # delegated install.py plan, not no-uv branch
    assert str(tmp_path / "app").lower() in out
    assert not (tmp_path / "app").exists()  # no install state created


def test_dry_run_honors_persisted_channel(tmp_path: Path) -> None:
    app = tmp_path / "app"
    write_marker(app)
    tarball = make_source_tarball(tmp_path / "source.tar.gz")
    result = run_ps(["-DryRun", "-SourceTarball", str(tarball)],
                    ps_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "channel main" in result.stdout.lower()


def test_explicit_channel_overrides_marker(tmp_path: Path) -> None:
    app = tmp_path / "app"
    write_marker(app)
    tarball = make_source_tarball(tmp_path / "source.tar.gz")
    result = run_ps(
        ["-DryRun", "-SourceTarball", str(tarball), "-Channel", "stable"],
        ps_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "channel stable" in result.stdout.lower()


def test_env_overrides_drive_the_iex_form(tmp_path: Path) -> None:
    # irm | iex cannot pass parameters: BRIDGE_* envs are the only path.
    tarball = make_source_tarball(tmp_path / "source.tar.gz")
    env = ps_env(tmp_path, BRIDGE_SOURCE_TARBALL=str(tarball), BRIDGE_DRYRUN="1")
    result = run_ps([], env)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout.lower()
    assert "dry-run" in out
    assert "channel stable" in out
    assert "would extract" in out  # delegated install.py plan
    assert not (tmp_path / "app").exists()


def test_missing_source_tarball_fails_fast(tmp_path: Path) -> None:
    result = run_ps(["-SourceTarball", str(tmp_path / "nope.tar.gz")],
                    ps_env(tmp_path))
    assert result.returncode != 0
    assert "re-run the one-liner" in result.stdout.lower()
    assert not (tmp_path / "app").exists()


def test_one_liner_iex_form_with_env_overrides(tmp_path: Path) -> None:
    # The headline delivery form: irm <url> | iex, driven by BRIDGE_* envs.
    tarball = make_source_tarball(tmp_path / "source.tar.gz")
    env = ps_env(tmp_path, BRIDGE_SOURCE_TARBALL=str(tarball), BRIDGE_DRYRUN="1")
    result = run_ps([], env,
                    command=f"Invoke-Expression (Get-Content -Raw '{SCRIPT}')")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry-run" in result.stdout.lower()
    assert "would extract" in result.stdout.lower()  # delegated plan
    assert not (tmp_path / "app").exists()


def test_dry_run_missing_override_still_plans(tmp_path: Path) -> None:
    result = run_ps(["-DryRun", "-SourceTarball", str(tmp_path / "nope.tar.gz")],
                    ps_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout.lower()
    assert "dry-run" in out
    assert "channel stable" in out
    assert not (tmp_path / "app").exists()


def test_iex_form_survives_dead_bridge_proxy_env(tmp_path: Path) -> None:
    # #59: the installer's own client hint sets HTTP(S)_PROXY at the
    # Bridge itself; with the tray stopped those vars dial a dead port.
    # The bootstrapper must strip them (and a foreign VIRTUAL_ENV) for
    # its own process. The discriminating sync pin lives at the run()
    # seam in test_install.py and in the CI smoke job (real sync).
    tarball = make_source_tarball(tmp_path / "source.tar.gz")
    dead = "http://127.0.0.1:9"  # nothing listens: connection refused
    env = ps_env(tmp_path, BRIDGE_SOURCE_TARBALL=str(tarball), BRIDGE_DRYRUN="1",
                 HTTP_PROXY=dead, HTTPS_PROXY=dead, ALL_PROXY=dead,
                 VIRTUAL_ENV=str(tmp_path / "foreign-venv"))
    result = run_ps([], env,
                    command=f"Invoke-Expression (Get-Content -Raw '{SCRIPT}')")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout.lower()
    assert "dry-run" in out
    assert "would extract" in out  # delegated install.py plan
    assert not (tmp_path / "app").exists()
    assert "virtual_env" not in (result.stdout + result.stderr).lower()
