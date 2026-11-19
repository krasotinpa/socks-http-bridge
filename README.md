# socks-http-bridge

A configurable HTTP→SOCKS5 bridge for Windows, living in the system tray.
Point HTTP-only clients at it and they exit through any SOCKS5 VPN.

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
  green — running, grey — stopped, red — error (Upstream unreachable).
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

Prerequisites:

- Windows with [uv](https://docs.astral.sh/uv/) and Python 3.13+ (uv can
  install Python for you).
- A VPN running with a local SOCKS5 proxy (the Upstream) — e.g. AdGuard VPN on
  `127.0.0.1:9090`. Any other SOCKS5 address works too: change it in Settings.

From the repo root:

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
   (single-instance mutex). No running process is ever stopped.
4. Verifies the first configured Listener (default `127.0.0.1:8118`) answers
   within ~10 s; on failure it prints the log path
   (`%LOCALAPPDATA%\socks-http-bridge\bridge.log`).

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

## Layout

- `src/socks_http_bridge/` — the application: tunneling core, tray, UI.
- `scripts/install.py` — idempotent installer.
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
