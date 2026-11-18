"""Exit IP Test fetch/parse core: exit IP plus country, city and ASN/org.

Standalone seam for the application shell (issue #6): explicit Listener
address and test_url parameters, no Config reads, no UI imports, stdlib
only. Every request goes through the Listener as an HTTP proxy (CONNECT
for https), i.e. through the Tunneling path; urllib's ProxyHandler does
the CONNECT itself.

Paths: ipinfo-style JSON from test_url is used directly; a plain IP gets
geo from ipinfo.io with an ip-api.com fallback. Failures raise
ExitIpError with a readable message, never a raw traceback.
"""

import http.client
import ipaddress
import json
import urllib.request
from dataclasses import dataclass

DEFAULT_TIMEOUT = 10.0
IPINFO_GEO_URL = "https://ipinfo.io/{ip}/json"
IP_API_GEO_URL = "http://ip-api.com/json/{ip}"


@dataclass(frozen=True)
class ExitIpResult:
    """The Exit IP Test outcome; geo fields may be absent."""

    ip: str
    country: str | None = None
    city: str | None = None
    org: str | None = None


class ExitIpError(Exception):
    """Readable Exit IP Test failure; the message is safe to show as-is."""


def _get(opener: urllib.request.OpenerDirector, url: str,
         timeout: float) -> bytes:
    with opener.open(url, timeout=timeout) as response:
        return response.read()


def fetch_exit_ip(proxy_address: tuple[str, int], test_url: str,
                  timeout: float = DEFAULT_TIMEOUT) -> ExitIpResult:
    """Fetch the exit IP seen through the Listener at ``proxy_address``."""
    proxy_url = "http://{}:{}".format(*proxy_address)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": proxy_url, "https": proxy_url}))
    try:
        body = _get(opener, test_url, timeout)
    except (OSError, http.client.HTTPException) as exc:
        raise ExitIpError(
            f"Exit IP Test request to {test_url} failed: {exc}") from exc
    text = body.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and "ip" in data:
        return ExitIpResult(ip=str(data["ip"]), country=data.get("country"),
                            city=data.get("city"), org=data.get("org"))
    # Not ipinfo JSON: maybe a bare IP, which needs a separate geo lookup.
    try:
        ipaddress.ip_address(text)
    except ValueError:
        raise ExitIpError(
            f"test_url returned neither ipinfo JSON nor an IP address: "
            f"{text[:80]!r}") from None
    return _geo_for_ip(opener, text, timeout)


def _geo_for_ip(opener: urllib.request.OpenerDirector, ip: str,
                timeout: float) -> ExitIpResult:
    failures = []
    for template in (IPINFO_GEO_URL, IP_API_GEO_URL):
        url = template.format(ip=ip)
        try:
            data = json.loads(_get(opener, url, timeout))
            if not isinstance(data, dict):
                # ValueError, not TypeError: handled like a JSON decode error.
                raise ValueError("geo response is not a JSON object")  # noqa: TRY004
            # ip-api reports failures in-band; ipinfo has no status field.
            if "status" in data and data["status"] != "success":
                raise ValueError(f"geo service status: {data['status']}")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            failures.append(f"{url}: {exc}")
            continue
        return ExitIpResult(ip=ip, country=data.get("country"),
                            city=data.get("city"),
                            org=data.get("org") or data.get("as"))
    raise ExitIpError(f"geo lookup for {ip} failed ({'; '.join(failures)})")
