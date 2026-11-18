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

## Layout

- `reference/bridge.py` — proven prototype the core is built from.
- `docs/adr/` — architecture decision records.
- `GLOSSARY.md` — domain language.
- `AGENTS.md` — rules for agents working in this repo.

## Status

In development. See GitHub Issues for the plan.
