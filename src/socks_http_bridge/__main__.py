"""Headless Bridge entry: local HTTP proxy Listeners -> SOCKS5 Upstream.

Exposes an HTTP/1.1 CONNECT proxy (and plain-HTTP forwarding) on every
Listener from Config and Tunnels every connection through the Upstream
with remote DNS resolution (SOCKS5h semantics).

Stdlib only. Run:
    pythonw.exe -m socks_http_bridge            (no console window)
    python.exe  -m socks_http_bridge --verbose  (console + debug)

Options:
    --config PATH        Config file (default %LOCALAPPDATA%\\socks-http-bridge\\config.json)
    --listen HOST:PORT   override the Config Listeners with a single Listener
    --socks  HOST:PORT   override the Config Upstream
    --log    PATH        override the Config log path
"""

import argparse
import logging
import os
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from socks_http_bridge.config import Config, ConfigError
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
    args = ap.parse_args()

    try:
        cfg = Config.load(Path(args.config) if args.config else None)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    # CLI flags override Config; without them Config alone drives the Bridge.
    listeners = [args.listen] if args.listen else cfg.listeners
    upstream = args.socks or cfg.upstream
    socks_host, _, socks_port_text = upstream.rpartition(":")
    socks_host = socks_host or "127.0.0.1"
    socks_port = int(socks_port_text)

    _setup_logging(args.log or os.path.expandvars(cfg.log_path), args.verbose)

    servers: list[socket.socket] = []
    for listen in listeners:
        listen_host, _, listen_port_text = listen.rpartition(":")
        listen_host = listen_host or "127.0.0.1"
        listen_port = int(listen_port_text)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((listen_host, listen_port))
        srv.listen(128)
        servers.append(srv)
        log.info("HTTP proxy on %s:%s -> SOCKS5 %s:%s",
                 listen_host, listen_port, socks_host, socks_port)

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
        for srv in servers:
            srv.close()


if __name__ == "__main__":
    main()
