"""Headless Bridge entry: local HTTP proxy Listener -> SOCKS5 Upstream.

Exposes an HTTP/1.1 CONNECT proxy (and plain-HTTP forwarding) on the Listener
and Tunnels every connection through the Upstream with remote DNS resolution
(SOCKS5h semantics). CLI preserved from reference/bridge.py.

Stdlib only. Run:
    pythonw.exe -m socks_http_bridge            (no console window)
    python.exe  -m socks_http_bridge --verbose  (console + debug)

Options:
    --listen HOST:PORT   default 127.0.0.1:8118
    --socks  HOST:PORT   default 127.0.0.1:9090
    --log    PATH        default %LOCALAPPDATA%\\socks-http-bridge\\bridge.log
"""

import argparse
import logging
import os
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler

from socks_http_bridge.tunneling import handle, log


def main() -> None:
    default_log = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "socks-http-bridge", "bridge.log")
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:8118")
    ap.add_argument("--socks", default="127.0.0.1:9090")
    ap.add_argument("--log", default=default_log)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    listen_host, _, listen_port_text = args.listen.rpartition(":")
    socks_host, _, socks_port_text = args.socks.rpartition(":")
    listen_host = listen_host or "127.0.0.1"
    listen_port = int(listen_port_text)
    socks_host = socks_host or "127.0.0.1"
    socks_port = int(socks_port_text)

    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(args.log, maxBytes=2_000_000, backupCount=2,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if args.verbose or sys.stderr is not None and sys.stderr.isatty():
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        log.addHandler(stream_handler)
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_host, listen_port))
    srv.listen(128)
    log.info("HTTP proxy on %s:%s -> SOCKS5 %s:%s",
             listen_host, listen_port, socks_host, socks_port)
    try:
        while True:
            client, addr = srv.accept()
            threading.Thread(target=handle,
                             args=(client, addr, socks_host, socks_port),
                             daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


if __name__ == "__main__":
    main()
