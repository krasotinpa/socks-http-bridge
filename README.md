# socks-http-bridge

A configurable HTTP→SOCKS5 bridge for Windows, living in the system tray.

## Why

AdGuard VPN exposes a local SOCKS5 proxy (default `127.0.0.1:9090`), but Codex CLI and
the ChatGPT-desktop Codex runtime speak `HTTP(S)_PROXY` reliably while their reqwest
HTTP client is built without SOCKS support (openai/codex#20844). This app exposes a
local HTTP CONNECT proxy (default `127.0.0.1:8118`) and tunnels every connection
through the SOCKS5 upstream with remote DNS resolution, so `HTTPS_PROXY=http://127.0.0.1:8118`
just works.

## What it does

- One `pythonw.exe` process, no console; tray icon shows state:
  green — running, grey — stopped, red — error (upstream unreachable).
- Tray menu: status (`listen → socks`) and exit-IP test, Stop/Start, Settings
  (multiple listen addresses, SOCKS upstream, Windows autostart, apply on the fly),
  open log, exit.
- Config: `%LOCALAPPDATA%\socks-http-bridge\config.json`.
- Autostart via a Startup shortcut managed from Settings; single-instance mutex.
- Core: CONNECT tunnels + plain-HTTP forwarding, SOCKS5 with remote DNS (ATYP=3),
  no idle timeout on tunnels (long-lived SSE/WebSocket), TCP_NODELAY, correct
  half-close, rotating log.

## Install

Prerequisites:

- [uv](https://docs.astral.sh/uv/) and Python 3.15 (on this laptop the
  install-manager shims at `%LOCALAPPDATA%\Python\bin` are stable).
- AdGuard VPN running with its SOCKS5 proxy on `127.0.0.1:9090` (the Upstream).

From the repo root:

```
uv run python scripts/install.py
```

Add `--python %LOCALAPPDATA%\Python\bin\python.exe` to
`uv run` if uv cannot find the install-manager Python on its own. Use
`--dry-run` first to print the planned actions without side effects.

What the script does, in order:

1. `uv sync` — dependencies first, so a broken sync aborts before anything
   else is touched. uv is found via `UV_BIN`, then `PATH`, then the
   install-manager layout under `%LOCALAPPDATA%\Python`.
2. Creates the Startup shortcut `SocksHttpBridge.lnk` targeting
   `.venv\Scripts\pythonw.exe -m socks_http_bridge --tray`. Other `.lnk`
   files are never touched.
3. Starts the tray app detached (no console). Re-runs are safe: an
   already-correct shortcut and an already-running tray are skipped
   (single-instance mutex). No running process is ever stopped.
4. Verifies the Listener `127.0.0.1:8118` answers within ~10 s; on failure
   it prints the log path (`%LOCALAPPDATA%\socks-http-bridge\bridge.log`).

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

Rollback: the old prototype stays in `tools/socks-http-bridge/bridge.py`.
Exit the tray app, remove the Startup shortcut (or untick Autostart in
Settings), and run the old `bridge.py` manually again.

## Layout

- `reference/bridge.py` — proven prototype the core is built from.
- `docs/adr/` — architecture decision records.
- `GLOSSARY.md` — domain language.
- `AGENTS.md` — rules for agents working in this repo.

## Status

In development. See GitHub Issues for the plan.
