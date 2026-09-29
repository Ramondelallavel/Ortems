"""Outbound network policy (webhooks, database connectors): SSRF protection.

User-configured URLs and hosts are a network surface. Every destination is resolved and *every*
resolved address is checked before connecting; the connection then goes to the checked address (not to
a second DNS answer — DNS rebinding):

* always refused unless explicitly allow-listed: loopback, link-local (169.254/16, fe80::/10 — includes
  cloud metadata endpoints), unspecified, multicast, reserved, and known metadata addresses;
* private networks (10/8, 172.16/12, 192.168/16, fc00::/7, 100.64/10): refused in production unless
  ``MONXU_EGRESS_PRIVATE=allow`` (on-premises installations reaching their own ERP), allowed in
  development unless ``MONXU_EGRESS_PRIVATE=deny``;
* ``MONXU_EGRESS_ALLOW``: comma-separated host names, addresses or CIDR ranges always allowed
  (e.g. ``erp.internal,10.20.0.0/16``);
* webhooks must use HTTPS in production; redirects are never followed.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import get_settings
from .errors import ValidationFailed

METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}
CGNAT = ipaddress.ip_network("100.64.0.0/10")


@dataclass(frozen=True)
class Destination:
    host: str
    port: int
    address: str  # the checked address to connect to


def _allow_list() -> tuple[set[str], list]:
    names: set[str] = set()
    nets = []
    for part in (os.environ.get("MONXU_EGRESS_ALLOW") or "").split(","):
        p = part.strip().lower()
        if not p:
            continue
        try:
            nets.append(ipaddress.ip_network(p, strict=False))
        except ValueError:
            names.add(p)
    return names, nets


def _private_allowed() -> bool:
    mode = (os.environ.get("MONXU_EGRESS_PRIVATE") or "").strip().lower()
    if mode in ("allow", "deny"):
        return mode == "allow"
    return not get_settings().is_production


def classify(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why an address is refused, or None."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return classify(ip.ipv4_mapped)
    if ip in METADATA:
        return "cloud metadata endpoint"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "reserved address"
    if ip.is_private or (isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT):
        return None if _private_allowed() else "private network address"
    return None


def check_host(host: str, port: int, purpose: str) -> Destination:
    """Resolve ``host`` and check every address; returns the address to connect to."""
    host = (host or "").strip().strip("[]")
    if not host:
        raise ValidationFailed(f"{purpose}: no host given.", code="EGRESS_DENIED")
    names, nets = _allow_list()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValidationFailed(f"{purpose}: the host {host} could not be resolved.", code="EGRESS_UNRESOLVED") from exc
    addrs = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip not in addrs:
            addrs.append(ip)
    if not addrs:
        raise ValidationFailed(f"{purpose}: the host {host} has no address.", code="EGRESS_UNRESOLVED")
    allowed_by_name = host.lower() in names
    for ip in addrs:
        if allowed_by_name or any(ip in n for n in nets):
            continue
        why = classify(ip)
        if why:
            raise ValidationFailed(
                f"{purpose}: {host} resolves to a {why} ({ip}), which outbound connections may not reach. An administrator can allow it with MONXU_EGRESS_ALLOW.",
                code="EGRESS_DENIED",
                context={"host": host, "address": str(ip)},
            )
    return Destination(host=host, port=port, address=str(addrs[0]))


def check_url(url: str, purpose: str) -> tuple[Destination, str]:
    """Check an HTTP(S) URL; returns the destination and the URL's scheme."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValidationFailed(f"{purpose}: only http(s) URLs are allowed.", code="EGRESS_DENIED")
    if scheme != "https" and get_settings().is_production:
        raise ValidationFailed(f"{purpose}: HTTPS is required.", code="EGRESS_DENIED")
    if parts.username or parts.password:
        raise ValidationFailed(f"{purpose}: credentials in the URL are not allowed (use the signing secret).", code="EGRESS_DENIED")
    port = parts.port or (443 if scheme == "https" else 80)
    return check_host(parts.hostname or "", port, purpose), scheme
