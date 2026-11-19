# Hosted PowerShell one-liner: install and update from source tarballs

ADR-0003 distributes source only, but the `git clone` + `scripts/install.py`
path assumes git, uv and terminal literacy — contributor-grade, not
end-user-grade. We add a hosted PowerShell bootstrapper in the astral uv
installer form (`irm | iex`), living at `scripts/install.ps1` on the public
repository's raw main branch: a thin, moving entry point that resolves the
channel at runtime, bootstraps uv when missing, fetches a source tarball
from GitHub codeload, and delegates to the install script's hosted mode.
No git, no gh, no embedded tokens; Python is provisioned by `uv sync`.
This extends ADR-0003 (source tarballs, not binaries) and keeps ADR-0002
(pure Python, no exe), while amending their update clause ("updating means
`git pull` and re-running the installer"): on the hosted path, updating
means re-running the one-liner; the git path keeps the old semantics.

## Consequences

- Install and update converge to one state: re-running the one-liner
  updates in place. Channels: stable = the latest release tag (GitHub
  API), main = the branch tarball with a commit-SHA marker. The explicit
  channel choice is persisted in `install-marker.json` in the app dir
  (`%LOCALAPPDATA%\socks-http-bridge\app`, `BRIDGE_INSTALL_DIR` override);
  a bare re-run honors it, an explicit override wins.
- Restart semantics: a fresh install never stops a running process; only
  an update stops the tray (graceful close attempt, then force kill; waits
  for process exit and mutex release), swaps the source with the virtual
  environment preserved, and relaunches (launch is skipped while another
  tray instance holds the single-instance mutex). The tarball is fully
  extracted before anything stops. Active Tunnels drop on update; clients reconnect
  on their own (the Apply precedent). Same target with the tray up is a
  no-op.
- Fail-fast with an actionable "re-run the one-liner" message; the re-run
  self-heals (re-fetch, swap, sync, launch). No rollback machinery.
- The `irm | iex` form cannot pass parameters, so every option has an
  environment override (`BRIDGE_CHANNEL`, `BRIDGE_DRYRUN`,
  `BRIDGE_INSTALL_DIR`, `UV_BIN`). `-SourceTarball` /
  `BRIDGE_SOURCE_TARBALL` bypasses the download through the same code
  path — the seam for offline installs and the planned CI E2E smoke job.
- The git install path is unchanged and coexists with the hosted app dir;
  the Startup shortcut pins whichever was installed last.
- Version identity stays ADR-0003's: the release tag matches the package
  version, and the stable channel resolves the latest release.
