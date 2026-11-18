#!/usr/bin/env python3
"""Local HTTP proxy -> SOCKS5 upstream (AdGuard VPN).

Exposes an HTTP/1.1 CONNECT proxy (and plain-HTTP forwarding) on 127.0.0.1:8118
and tunnels every connection through the AdGuard VPN SOCKS5 listener
(default 127.0.0.1:9090) with remote DNS resolution (SOCKS5h semantics).

Why: Codex CLI / ChatGPT-desktop Codex runtime speak HTTP(S)_PROXY reliably,
while their reqwest HTTP client is built without SOCKS support (openai/codex
issue #20844), so a local HTTP->SOCKS5 bridge is the stable arrangement.

Stdlib only. Run:
    pythonw.exe bridge.py            (no console window)
    python.exe  bridge.py --verbose  (console + debug)

Options:
    --listen HOST:PORT   default 127.0.0.1:8118
    --socks  HOST:PORT   default 127.0.0.1:9090
    --log    PATH        default %LOCALAPPDATA%\\socks-http-bridge\\bridge.log
"""

import argparse
import logging
import os
import socket
import struct
import sys
import threading
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit

BUF = 65536
log = logging.getLogger("bridge")


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("upstream closed during SOCKS5 handshake")
        data += chunk
    return data


def socks5_connect(socks_host, socks_port, dst_host, dst_port, timeout=25):
    """Open a TCP connection to dst_host:dst_port through the SOCKS5 proxy.
    Domain names are sent as-is (ATYP=3) so DNS resolves on the VPN side."""
    s = socket.create_connection((socks_host, socks_port), timeout=timeout)
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(b"\x05\x01\x00")  # version 5, 1 method, no-auth
        if recv_exact(s, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS5 proxy rejected no-auth method")
        try:
            hostb = dst_host.encode("idna")
        except UnicodeError:
            hostb = dst_host.encode("ascii")
        if len(hostb) > 255:
            raise ConnectionError("hostname too long for SOCKS5")
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(hostb)]) + hostb
                  + struct.pack(">H", dst_port))
        head = recv_exact(s, 4)
        if head[1] != 0:
            raise ConnectionError(f"SOCKS5 connect failed, rep={head[1]}")
        atyp = head[3]
        if atyp == 1:
            recv_exact(s, 4)
        elif atyp == 3:
            recv_exact(s, recv_exact(s, 1)[0])
        elif atyp == 4:
            recv_exact(s, 16)
        recv_exact(s, 2)  # bound port
        s.settimeout(None)  # long-lived streams (SSE / websocket) must not time out
        return s
    except Exception:
        s.close()
        raise


def pipe(src, dst):
    try:
        while True:
            data = src.recv(BUF)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def read_headers(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(BUF)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            raise ConnectionError("client headers too large")
    return data


def handle(client, addr, socks_host, socks_port):
    remote = None
    try:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.settimeout(30)
        raw = read_headers(client)
        if not raw:
            return
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split()
        if len(parts) < 3:
            return
        method, target = parts[0], parts[1]

        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            port = int(port or 443)
            remote = socks5_connect(socks_host, socks_port, host, port)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            log.info("CONNECT %s:%s", host, port)
        else:
            url = urlsplit(target)
            if not url.hostname:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            host = url.hostname
            port = url.port or 80
            remote = socks5_connect(socks_host, socks_port, host, port)
            path = url.path or "/"
            if url.query:
                path += "?" + url.query
            fwd = [f"{method} {path} {parts[2]}"]
            for line in lines[1:]:
                name = line.split(":", 1)[0].strip().lower()
                if name in ("proxy-connection", "proxy-authenticate",
                            "proxy-authorization"):
                    continue
                fwd.append(line)
            remote.sendall(("\r\n".join(fwd) + "\r\n\r\n").encode("latin-1")
                           + body)
            log.info("%s %s://%s%s", method, url.scheme, host, path)

        client.settimeout(None)
        t1 = threading.Thread(target=pipe, args=(client, remote), daemon=True)
        t2 = threading.Thread(target=pipe, args=(remote, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception as exc:
        log.warning("%s -> %s", addr[0], exc)
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except OSError:
            pass
    finally:
        for s in (client, remote):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def main():
    default_log = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "socks-http-bridge", "bridge.log")
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:8118")
    ap.add_argument("--socks", default="127.0.0.1:9090")
    ap.add_argument("--log", default=default_log)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    lh, _, lp = args.listen.rpartition(":")
    sh, _, sp = args.socks.rpartition(":")
    lh, lp, sh, sp = lh or "127.0.0.1", int(lp), sh or "127.0.0.1", int(sp)

    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(args.log, maxBytes=2_000_000, backupCount=2,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if args.verbose or sys.stderr is not None and sys.stderr.isatty():
        sh_ = logging.StreamHandler()
        sh_.setFormatter(fmt)
        log.addHandler(sh_)
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((lh, lp))
    srv.listen(128)
    log.info("HTTP proxy on %s:%s -> SOCKS5 %s:%s", lh, lp, sh, sp)
    try:
        while True:
            client, addr = srv.accept()
            threading.Thread(target=handle,
                             args=(client, addr, sh, sp),
                             daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


if __name__ == "__main__":
    main()
