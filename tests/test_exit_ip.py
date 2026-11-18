"""Integration tests at the Exit IP Test seam: fetch/parse through a real
Bridge Listener + fake SOCKS5 Upstream + canned targets, all on ephemeral
ports. The live Bridge (127.0.0.1:8118) and AdGuard (127.0.0.1:9090) are
never touched.

test_urls are http:// through the Tunnel; the https geo URL is switched to
http:// on the same ipinfo.io host (the fake Upstream routes by CONNECT
host). The https CONNECT+TLS plumbing is covered by test_tunneling and the
manual acceptance in issue #6.
"""

import json
import socket
import time

import pytest
from fake_upstream import FakeUpstream
from targets import RecordingHttpTarget, SilentTarget

from socks_http_bridge import exit_ip
from socks_http_bridge.exit_ip import ExitIpError, ExitIpResult, fetch_exit_ip


def dead_port() -> int:
    """Reserve an ephemeral port and release it: nothing listens there."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_json_test_url_returns_ipinfo_fields_directly(start_bridge):
    payload = json.dumps({"ip": "203.0.113.7", "country": "NL",
                          "city": "Amsterdam", "org": "AS12345 Example VPN"}
                         ).encode("utf-8")
    with RecordingHttpTarget(payload) as target, \
            FakeUpstream(target.address) as upstream:
        listener = start_bridge(upstream.address)
        result = fetch_exit_ip(listener, "http://exit.test/json")
        assert result == ExitIpResult(ip="203.0.113.7", country="NL",
                                      city="Amsterdam",
                                      org="AS12345 Example VPN")
        # The JSON is used directly: one request, no geo lookup.
        assert [r.host for r in upstream.requests] == [b"exit.test"]


def test_plain_ip_test_url_fetches_geo_from_ipinfo(start_bridge, monkeypatch):
    # Offline: the ipinfo geo hop uses the same plumbing over http:// (the
    # https CONNECT+TLS wrap is manually accepted in issue #6). The host is
    # unchanged so the fake Upstream's host routing still proves which
    # endpoint was contacted.
    monkeypatch.setattr(exit_ip, "IPINFO_GEO_URL", "http://ipinfo.io/{ip}/json")
    geo = json.dumps({"ip": "203.0.113.7", "country": "DE", "city": "Berlin",
                      "org": "AS67890 Example ISP"}).encode("utf-8")
    with RecordingHttpTarget(b"203.0.113.7\n") as ip_target, \
            RecordingHttpTarget(geo) as geo_target, \
            FakeUpstream(ip_target.address,
                         routes={"ipinfo.io": geo_target.address}) as upstream:
        listener = start_bridge(upstream.address)
        result = fetch_exit_ip(listener, "http://exit.test/ip")
        assert result == ExitIpResult(ip="203.0.113.7", country="DE",
                                      city="Berlin", org="AS67890 Example ISP")
        assert [r.host for r in upstream.requests] == [b"exit.test", b"ipinfo.io"]
        # The geo request carries the exit IP in the path.
        geo_head = geo_target.requests[0][0].decode("latin-1")
        assert geo_head.split("\r\n")[0] == "GET /203.0.113.7/json HTTP/1.1"


def test_plain_ip_falls_back_to_ip_api_when_ipinfo_fails(start_bridge,
                                                         monkeypatch):
    monkeypatch.setattr(exit_ip, "IPINFO_GEO_URL", "http://ipinfo.io/{ip}/json")
    # ip-api.com is already http:// in the module, so it needs no patch.
    ip_api_geo = json.dumps({"status": "success", "country": "FR",
                             "city": "Paris", "org": "AS1234 Some ISP",
                             "query": "203.0.113.7"}).encode("utf-8")
    with RecordingHttpTarget(b"203.0.113.7") as ip_target, \
            RecordingHttpTarget(ip_api_geo) as ip_api_target, \
            FakeUpstream(ip_target.address,
                         routes={"ipinfo.io": ("127.0.0.1", dead_port()),
                                 "ip-api.com": ip_api_target.address}) as upstream:
        listener = start_bridge(upstream.address)
        result = fetch_exit_ip(listener, "http://exit.test/ip")
        assert result == ExitIpResult(ip="203.0.113.7", country="FR",
                                      city="Paris", org="AS1234 Some ISP")
        # ipinfo was tried first, then the ip-api fallback.
        assert [r.host for r in upstream.requests] == [
            b"exit.test", b"ipinfo.io", b"ip-api.com"]
        geo_head = ip_api_target.requests[0][0].decode("latin-1")
        assert geo_head.split("\r\n")[0] == "GET /json/203.0.113.7 HTTP/1.1"


def test_dead_upstream_yields_readable_error_not_traceback(start_bridge):
    listener = start_bridge(("127.0.0.1", dead_port()))
    with pytest.raises(ExitIpError) as excinfo:
        fetch_exit_ip(listener, "http://exit.test/ip")
    message = str(excinfo.value)
    assert "http://exit.test/ip" in message
    assert "Traceback" not in message


def test_bad_payload_yields_readable_error(start_bridge):
    with RecordingHttpTarget(b"this is not json or an ip") as target, \
            FakeUpstream(target.address) as upstream:
        listener = start_bridge(upstream.address)
        with pytest.raises(ExitIpError) as excinfo:
            fetch_exit_ip(listener, "http://exit.test/bad")
        assert "neither ipinfo JSON nor an IP address" in str(excinfo.value)


def test_timeout_is_honoured_with_readable_error(start_bridge):
    # Explicit small timeout so the test stays fast; the target stays silent
    # far longer than it, proving the client read timeout fires.
    with SilentTarget() as target, FakeUpstream(target.address) as upstream:
        listener = start_bridge(upstream.address)
        started = time.monotonic()
        with pytest.raises(ExitIpError) as excinfo:
            fetch_exit_ip(listener, "http://exit.test/ip", timeout=0.5)
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"timeout not honoured, took {elapsed:.1f}s"
        assert "timed out" in str(excinfo.value).lower()


def test_both_geo_sources_failing_yields_readable_error(start_bridge,
                                                        monkeypatch):
    monkeypatch.setattr(exit_ip, "IPINFO_GEO_URL", "http://ipinfo.io/{ip}/json")
    ip_api_fail = json.dumps({"status": "fail",
                              "message": "reserved range"}).encode("utf-8")
    with RecordingHttpTarget(b"203.0.113.7") as ip_target, \
            RecordingHttpTarget(ip_api_fail) as ip_api_target, \
            FakeUpstream(ip_target.address,
                         routes={"ipinfo.io": ("127.0.0.1", dead_port()),
                                 "ip-api.com": ip_api_target.address}) as upstream:
        listener = start_bridge(upstream.address)
        with pytest.raises(ExitIpError) as excinfo:
            fetch_exit_ip(listener, "http://exit.test/ip")
        message = str(excinfo.value)
        assert "203.0.113.7" in message
        assert "ipinfo.io" in message and "ip-api.com" in message


def test_defaults_match_the_issue_spec():
    # The geo tests downgrade ipinfo to http:// offline; pin the production
    # values here (https CONNECT+TLS is manually accepted in issue #6).
    assert exit_ip.DEFAULT_TIMEOUT == 10.0
    assert exit_ip.IPINFO_GEO_URL == "https://ipinfo.io/{ip}/json"
    assert exit_ip.IP_API_GEO_URL == "http://ip-api.com/json/{ip}"
