# socks-http-bridge

A configurable HTTP→SOCKS5 bridge for Windows, living in the system tray.
Point HTTP-only clients at it and they exit through any SOCKS5 VPN.

[![CI](https://github.com/krasotinpa/socks-http-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/krasotinpa/socks-http-bridge/actions/workflows/ci.yml)

## Why

Some HTTP-only clients cannot use a SOCKS5 proxy directly: Codex CLI and the
ChatGPT-desktop Codex runtime speak `HTTP(S)_PROXY` reliably, but their reqwest
HTTP client is built without SOCKS support (openai/codex#20844). Many VPN apps,
in turn, expose only a local SOCKS5 proxy — e.g. AdGuard VPN, default
`127.0.0.1:9090`.

The Bridge closes the gap: it exposes local HTTP CONNECT Listeners (default
`127.0.0.1:8118`) and tunnels every connection through the SOCKS5 Upstream with
remote DNS resolution, so `HTTPS_PROXY=http://127.0.0.1:8118` just works — with
any VPN that provides a local SOCKS5 proxy.

## What it does

- One `pythonw.exe` process, no console; tray icon shows state:
  green — running, amber — degraded (a Listener could not bind, the others
  serve), grey — stopped, red — error (no Listener bound or Upstream
  unreachable). Hover the icon for per-Listener and Upstream status.
- Tray menu: the `listen → socks` address line, Stop/Start, Open log,
  Exit IP Test, Settings (multiple Listeners, SOCKS Upstream, Windows
  Autostart, Apply on the fly), About… (name, version, license, repository
  link), Exit.
- Config: `%LOCALAPPDATA%\socks-http-bridge\config.json`.
- Autostart via a Startup shortcut managed from Settings; single-instance mutex.
- Core: CONNECT tunnels + plain-HTTP forwarding, SOCKS5 with remote DNS (ATYP=3),
  no idle timeout on tunnels (long-lived SSE/WebSocket), TCP_NODELAY, correct
  half-close, rotating log.

## Install

### One-liner (recommended)

Windows only. No git, no Python, no uv needed up front — the bootstrapper
acquires uv (and Python through uv) itself. A VPN with a local SOCKS5 proxy
(the Upstream) must be running — e.g. AdGuard VPN on `127.0.0.1:9090`; any
other SOCKS5 address works too, change it in Settings.

Paste into PowerShell:

```
powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://raw.githubusercontent.com/krasotinpa/socks-http-bridge/main/scripts/install.ps1 | iex"
```

The Bridge is installed to `%LOCALAPPDATA%\socks-http-bridge\app`, the
Startup shortcut `SocksHttpBridge.lnk` is created, the tray starts (unless
another tray instance already holds the single-instance mutex), and the
first configured Listener (default `127.0.0.1:8118`) is verified. The
summary prints the client environment variables and the Exit IP Test hint. Failures are fail-fast: re-run the same one-liner to
retry.

The `irm | iex` form cannot pass parameters — environment variables are the
overrides. Set `$env:BRIDGE_DRYRUN = '1'` before the one-liner to print the
plan without changes.

### Update

Re-run the same one-liner — install and update converge:

- An up-to-date re-run with the tray running is a no-op: nothing is stopped.
- An update restarts the tray to load the new code: **active Tunnels drop
  and clients reconnect on their own**, as with Apply. Update at a
  convenient moment.
- A fresh install never stops a running process; only an update restarts
  the tray.

Channels: `stable` (the latest GitHub Release) is the default; `main` (the
latest committed code) is an explicit choice. Set `$env:BRIDGE_CHANNEL =
'main'` before the one-liner to switch. The choice is persisted in the
install marker, so a bare re-run never silently switches a `main` install
back to `stable`.

### From source (contributors)

Prerequisites: Windows with [uv](https://docs.astral.sh/uv/) and Python
3.13+ (uv can install Python for you), and a VPN with a local SOCKS5 proxy
(the Upstream) running. From the repo root:

```
uv run python scripts/install.py
```

Use `--dry-run` first to print the planned actions without side effects. If uv
cannot find Python on its own, point it at your interpreter:
`uv sync --python <path-to-python.exe>`.

What the script does, in order:

1. `uv sync` — dependencies first, so a broken sync aborts before anything
   else is touched. uv is found via `UV_BIN`, then `PATH`, then the
   install-manager layout under `%LOCALAPPDATA%\Python`.
2. Creates the Startup shortcut `SocksHttpBridge.lnk` targeting
   `.venv\Scripts\pythonw.exe -m socks_http_bridge --tray`. Other `.lnk`
   files are never touched.
3. Starts the tray app detached (no console). Re-runs are safe: an
   already-correct shortcut and an already-running tray are skipped
   (single-instance mutex). The script never stops a running process:
   after `git pull`, Exit the tray and re-run to load new code.
4. Verifies the first configured Listener (default `127.0.0.1:8118`) answers
   within ~10 s; on failure it prints the log path
   (`%LOCALAPPDATA%\socks-http-bridge\bridge.log`).

The git checkout and the hosted app dir coexist; the Startup shortcut pins
whichever was installed last.

### Configure clients

Point HTTP-only clients at the Bridge (no `ALL_PROXY`):

```
HTTP_PROXY=http://127.0.0.1:8118
HTTPS_PROXY=http://127.0.0.1:8118
NO_PROXY=localhost,127.0.0.1,::1
```

Verify the exit IP is the VPN's:

```
curl -x http://127.0.0.1:8118 https://api.ipify.org
```

### Uninstall

Manual (an uninstall flag is deferred):

1. Tray menu → Exit.
2. Remove the Startup shortcut: Settings → uncheck Autostart, or delete
   `SocksHttpBridge.lnk` from the Startup folder (Win+R → `shell:startup`).
3. Delete the app dir `%LOCALAPPDATA%\socks-http-bridge\app`. Config and log
   live one level up (`%LOCALAPPDATA%\socks-http-bridge`) and are shared with
   any coexisting from-source install — delete that directory too only if
   you want the Bridge fully gone.

## Layout

- `src/socks_http_bridge/` — the application: tunneling core, tray, UI.
- `scripts/install.py` — idempotent installer and hosted update core.
- `scripts/install.ps1` — hosted one-liner bootstrapper (install/update).
- `tests/` — hermetic test suite (ephemeral ports, no live Bridge or Upstream).
- `docs/adr/` — architecture decision records.
- `GLOSSARY.md` — domain language.
- `CONTRIBUTING.md` — how to contribute.
- `AGENTS.md` — rules for agents working in this repo.

## License

MIT — see [LICENSE](LICENSE).

## Status

In active development. See [GitHub Releases](https://github.com/krasotinpa/socks-http-bridge/releases)
for released versions and source artifacts (sdist + wheel).
