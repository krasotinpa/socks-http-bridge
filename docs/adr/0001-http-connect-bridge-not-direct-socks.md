# HTTP CONNECT bridge instead of direct SOCKS for Codex

AdGuard VPN exposes only a local SOCKS5 proxy, but Codex CLI / the ChatGPT-desktop
Codex runtime cannot use SOCKS: their reqwest HTTP client is built without the
`socks` feature (openai/codex#20844), verified on this machine (`socks5h://` env →
"workspace routing discovery failed"). We therefore expose a local HTTP CONNECT
proxy and tunnel through SOCKS5 ourselves, with remote DNS (ATYP=3) so names
resolve on the VPN side.

## Considered Options

- Direct `HTTPS_PROXY=socks5h://…` — fails, reqwest has no SOCKS support.
- A Go proxy binary (gost) — Windows Defender deleted it on sight; AV risk.
- System-wide proxy settings — affects every application, not just Codex.

## Consequences

Clients must be pointed at the Bridge with `HTTP_PROXY=HTTPS_PROXY=http://127.0.0.1:8118`
(no `ALL_PROXY`). The Bridge core must keep long-lived tunnels alive without idle
timeouts (Codex SSE/WebSocket streams).
