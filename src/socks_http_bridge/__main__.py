"""Headless Bridge entry: local HTTP proxy Listeners -> SOCKS5 Upstream.

Exposes an HTTP/1.1 CONNECT proxy (and plain-HTTP forwarding) on every
Listener from Config and Tunnels every connection through the Upstream
with remote DNS resolution (SOCKS5h semantics).

Stdlib only. Run:
    pythonw.exe -m socks_http_bridge            (no console window)
    python.exe  -m socks_http_bridge --verbose  (console + debug)
    pythonw.exe -m socks_http_bridge --tray     (Tray Shell: icon + menu)

Options:
    --config PATH        Config file (default %LOCALAPPDATA%\\socks-http-bridge\\config.json)
    --listen HOST:PORT   override the Config Listeners with a single Listener
    --socks  HOST:PORT   override the Config Upstream
    --log    PATH        override the Config log path
    --tray               run as the Tray Shell (single instance, tray icon)
"""

import argparse
import logging
import os
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from socks_http_bridge.config import Config, ConfigError, parse_address
from socks_http_bridge.health import BridgeHealth
from socks_http_bridge.tunneling import handle, log


def _setup_logging(log_path: str, verbose: bool) -> None:
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=2,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if verbose or sys.stderr is not None and sys.stderr.isatty():
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        log.addHandler(stream_handler)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--listen", default=None)
    ap.add_argument("--socks", default=None)
    ap.add_argument("--log", default=None)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--tray", action="store_true")
    args = ap.parse_args()

    try:
        cfg = Config.load(Path(args.config) if args.config else None)
        # CLI flags override Config; every address passes the same validation.
        listeners = [args.listen] if args.listen else cfg.listeners
        upstream = args.socks or cfg.upstream
        socks_host, socks_port = parse_address(
            upstream, "--socks" if args.socks else "upstream")
        addresses = [
            parse_address(listen, "--listen" if args.listen else "listeners")
            for listen in listeners]
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    _setup_logging(args.log or os.path.expandvars(cfg.log_path), args.verbose)

    if args.tray:
        # Lazy import: the headless path stays stdlib-only (no pystray/PIL).
        from socks_http_bridge import tray
        tray.run(cfg, addresses, (socks_host, socks_port),
                 args.log or os.path.expandvars(cfg.log_path))
        return

    health = BridgeHealth((socks_host, socks_port),
                          probe_interval=cfg.probe_interval)
    servers: list[socket.socket] = []
    bind_failed = False
    for listen_host, listen_port in addresses:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((listen_host, listen_port))
            srv.listen(128)
        except OSError as exc:
            # A taken port must not kill the Bridge: the other Listeners
            # keep serving, and Bridge State reports Degraded.
            srv.close()
            log.error("Listener %s:%s failed to bind: %s",
                      listen_host, listen_port, exc)
            bind_failed = True
            continue
        servers.append(srv)
        log.info("HTTP proxy on %s:%s -> SOCKS5 %s:%s",
                 listen_host, listen_port, socks_host, socks_port)
    if not servers:
        log.error("No Listener could take its port; exiting")
        sys.exit(1)
    health.start()
    if bind_failed:
        # After start(): start() resets Listener failures, a new attempt.
        # Some Listeners serve (none bound exits above): Degraded.
        health.listener_failed(partial=True)

    def _accept_loop(srv: socket.socket) -> None:
        while True:
            client, addr = srv.accept()
            threading.Thread(target=handle,
                             args=(client, addr, socks_host, socks_port),
                             daemon=True).start()

    try:
        threads = [threading.Thread(target=_accept_loop, args=(srv,), daemon=True)
                   for srv in servers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        pass
    finally:
        health.stop()
        for srv in servers:
            srv.close()


if __name__ == "__main__":
    main()
