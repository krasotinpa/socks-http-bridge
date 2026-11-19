# Socks HTTP Bridge

A Windows tray application that lets HTTP-only clients (Codex CLI, ChatGPT desktop)
use a SOCKS5-only VPN exit (e.g. AdGuard VPN) by bridging local HTTP proxy listeners to
one SOCKS5 upstream.

## Language

**Bridge**:
The whole application process that mediates between HTTP clients and the SOCKS5
upstream.
_Avoid_: proxy (ambiguous — the Upstream is also a proxy), мост

**Listener**:
A host:port where the Bridge accepts HTTP client connections. The Bridge has one
or more Listeners, all sharing one Upstream.
_Avoid_: bind, endpoint, вход

**Upstream**:
The single SOCKS5 proxy — a VPN's local SOCKS5 listener, e.g. AdGuard VPN —
that every Tunnel is carried through.
_Avoid_: SOCKS server, target, выход

**Tunnel**:
One paired connection between an HTTP client and a remote host, carried through
the Upstream. Long-lived Tunnels (SSE, WebSocket) are first-class.
_Avoid_: session, pipe, соединение

**Probe**:
The periodic TCP-connect reachability check of the Upstream that feeds Bridge
State.
_Avoid_: healthcheck, ping

**Bridge State**:
One of Running (Listeners serve and the Upstream answers Probes), Stopped (the
user stopped the Bridge), Error (running but the Upstream is unreachable, or a
Listener failed to take its port).
_Avoid_: status (overloaded), mode

**Exit IP Test**:
A user-initiated one-shot request through the Bridge that reveals the public exit
IP together with its country and city.
_Avoid_: IP check, speedtest

**Apply**:
Committing Config changes by restarting the Listeners immediately; active
Tunnels are dropped and clients reconnect on their own.
_Avoid_: save (saving the file is only part of Apply), reload

**Autostart**:
Launching the Bridge at Windows logon via a Startup shortcut managed from the
settings window.
_Avoid_: autorun, boot

**Config**:
The persisted settings of the Bridge: Listeners, Upstream, Autostart, Probe
interval, log path, and Exit IP Test URL.
_Avoid_: settings (the window is Settings; the data is Config), preferences
