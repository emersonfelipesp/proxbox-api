"""SSRF protection utilities for validating endpoint configurations.

SSRF protection validates that endpoints do not point to restricted IP addresses.
Settings can be configured via NetBox plugin (ProxboxPluginSettings) with caching.

IPs that are already registered in ProxmoxEndpoint or NetBoxEndpoint are automatically allowed.
IPs submitted during endpoint creation/update are pre-allowed so that the endpoint's
own address passes SSRF validation within the same request.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from netbox_sdk.facade import Api

logger = logging.getLogger(__name__)

INTERNAL_IP_RANGES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

PRIVATE_IP_RANGES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

_registered_ips_cache: set[str] = set()
_registered_domains_cache: set[str] = set()


def get_registered_endpoints() -> tuple[set[str], set[str]]:
    """Get all IPs and domains from registered endpoints in the database.

    Returns (ips, domains) tuple of sets.
    Cached in memory to avoid repeated DB queries.
    """
    global _registered_ips_cache, _registered_domains_cache

    if _registered_ips_cache or _registered_domains_cache:
        return _registered_ips_cache, _registered_domains_cache

    from sqlmodel import Session, select

    from proxbox_api.database import NetBoxEndpoint, ProxmoxEndpoint, get_engine

    ips: set[str] = set()
    domains: set[str] = set()

    try:
        with Session(get_engine()) as session:
            for endpoint in session.exec(select(ProxmoxEndpoint)).all():
                if endpoint.ip_address:
                    ips.add(endpoint.ip_address.split("/")[0])
                if endpoint.domain:
                    domains.add(endpoint.domain.strip().lower())

            for endpoint in session.exec(select(NetBoxEndpoint)).all():
                if endpoint.ip_address:
                    ips.add(endpoint.ip_address.split("/")[0])
                if endpoint.domain:
                    domains.add(endpoint.domain.strip().lower())

    except Exception as exc:
        # Do not cache a failed load as authoritative: return an empty result so
        # the next call retries the database. Log only the exception type, since
        # the message may embed connection details.
        logger.warning(
            "SSRF: could not load registered endpoints from the database (%s); "
            "registered-endpoint allow-list is unavailable for this check",
            type(exc).__name__,
        )
        return set(), set()

    _registered_ips_cache = ips
    _registered_domains_cache = domains
    return ips, domains


def clear_endpoint_cache() -> None:
    """Clear the registered endpoints cache.

    Call this after creating/updating/deleting endpoints.
    """
    global _registered_ips_cache, _registered_domains_cache
    _registered_ips_cache = set()
    _registered_domains_cache = set()


def pre_allow_endpoint_hosts(*hosts: str, source: str = "endpoint") -> None:
    """Pre-seed the SSRF cache with IPs/domains from an endpoint being created or updated.

    Called before SSRF validation in create/update handlers so that the endpoint's
    own address is treated as registered and passes validation in the same request.
    Any internal or reserved address is automatically allowed and logged — users
    are not asked to configure SSRF manually for their own endpoint addresses.

    Args:
        *hosts: IP address strings or domain names to allow.
        source: Label used in log messages (e.g. "NetBox", "Proxmox").
    """
    global _registered_ips_cache, _registered_domains_cache

    for host in hosts:
        if not host:
            continue
        host = host.strip()
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
            if host not in _registered_ips_cache:
                _registered_ips_cache.add(host)
                logger.info(
                    "SSRF: auto-allowed IP %s from %s endpoint configuration",
                    host,
                    source,
                )
        except ValueError:
            host_lower = host.lower()
            if host_lower not in _registered_domains_cache:
                _registered_domains_cache.add(host_lower)
                logger.info(
                    "SSRF: auto-allowed domain %s from %s endpoint configuration",
                    host,
                    source,
                )


def is_registered_endpoint(host: str) -> bool:
    """Check if host is already registered in ProxmoxEndpoint or NetBoxEndpoint."""
    ips, domains = get_registered_endpoints()
    host_lower = host.strip().lower()

    if host_lower in domains:
        return True

    try:
        ipaddress.ip_address(host)
        return host in ips
    except ValueError:
        pass

    return False


def _normalize_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unwrap IPv6 forms that embed an IPv4 address so IPv4 range checks apply."""
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        if addr.sixtofour is not None:
            return addr.sixtofour
        if addr.teredo is not None:
            return addr.teredo[1]
    return addr


def _looks_like_ip_literal(host: str) -> bool:
    """Return True if host is shaped like an IP literal rather than a hostname."""
    return ":" in host or "%" in host or bool(re.fullmatch(r"[0-9.]+", host))


def is_ip_blocked(  # noqa: C901
    ip: str,
    settings: dict | None = None,
) -> tuple[bool, str]:
    """Check if an IP address is blocked based on settings.

    Returns (is_blocked, reason).
    """
    try:
        original = ipaddress.ip_address(ip)
    except ValueError:
        if _looks_like_ip_literal(ip):
            return True, f"Host '{ip}' looks like an IP address but could not be parsed"
        return False, "OK"
    # Evaluate both the address as written and the IPv4 it embeds (mapped, 6to4,
    # Teredo): explicit IPv6 policy keeps applying to the original, and IPv4 policy
    # applies to the embedded address.
    addr = _normalize_ip(original)
    candidates = (original,) if addr == original else (original, addr)

    # If IP is already registered, allow it
    if is_registered_endpoint(ip):
        return False, "OK"

    if settings is None:
        settings = {}

    ssrf_enabled = settings.get("ssrf_protection_enabled", True)
    if not ssrf_enabled:
        return False, "OK"

    blocked_ranges = settings.get("blocked_ip_ranges", [])
    for network in blocked_ranges:
        if any(candidate in network for candidate in candidates):
            return True, f"Host '{ip}' is in explicitly blocked range {network}"

    # Operator allow-lists apply to the address as written and to a true IPv4-mapped
    # form only. 6to4 and Teredo addresses merely embed an IPv4 and reach it through a
    # relay, so an IPv4 allow range must not whitelist them.
    allow_candidates = (original,)
    if isinstance(original, ipaddress.IPv6Address) and original.ipv4_mapped is not None:
        allow_candidates = (original, original.ipv4_mapped)
    allowed_ranges = settings.get("allowed_ip_ranges", [])
    for network in allowed_ranges:
        if any(candidate in network for candidate in allow_candidates):
            return False, "OK"

    allow_private = settings.get("allow_private_ips", True)
    if allow_private:
        for network in PRIVATE_IP_RANGES:
            if addr in network:
                return False, "OK"

    for network in INTERNAL_IP_RANGES:
        if any(candidate in network for candidate in candidates):
            return (
                True,
                f"Host '{ip}' is a reserved/internal IP address. Either add it to ProxmoxEndpoint first, or adjust SSRF settings in ProxboxPluginSettings.",
            )

    return False, "OK"


def validate_endpoint_host(  # noqa: C901
    host: str | None,
    settings: dict | None = None,
) -> tuple[bool, str]:
    """Validate that an endpoint host is not an internal/reserved IP.

    Returns (is_safe, reason).

    Settings are fetched from NetBox plugin with caching:
    - ssrf_protection_enabled: bool (default True)
    - allow_private_ips: bool (default True for on-premises)
    - allowed_ip_ranges: list of CIDR strings
    - blocked_ip_ranges: list of CIDR strings

    IPs already registered in ProxmoxEndpoint or NetBoxEndpoint are automatically allowed.
    """
    if not host:
        return False, "Host cannot be empty"

    host = host.strip()

    # If domain/host is already registered, allow it
    if is_registered_endpoint(host):
        return True, "OK"

    blocked, reason = is_ip_blocked(host, settings)
    if blocked:
        return False, reason

    try:
        ipaddress.ip_address(host)
        return True, "OK"
    except ValueError:
        pass

    if host.lower() in ("localhost", "localhost.localdomain"):
        return False, "localhost is not allowed"

    if re.match(r"^[\w.-]+\.(local|lan|internal|private)$", host, re.IGNORECASE):
        return False, f"Host '{host}' appears to be an internal domain"

    # Hostname did not match any heuristic block. Resolve it and re-validate every
    # resolved IP so that a hostname pointing at e.g. 127.0.0.1 cannot bypass the
    # IP-based SSRF guard.
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        return False, f"Could not resolve host '{host}': {exc}"

    resolved: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            resolved.add(str(sockaddr[0]))
    if not resolved:
        return False, f"Could not resolve host '{host}'"

    for resolved_ip in resolved:
        blocked, reason = is_ip_blocked(resolved_ip, settings)
        if blocked:
            return False, f"Host '{host}' resolves to {resolved_ip}: {reason}"

    return True, "OK"


def validate_endpoint_url(url: str | None, settings: dict | None = None) -> tuple[bool, str]:
    """Validate that a URL doesn't point to an internal resource."""
    if not url:
        return False, "URL cannot be empty"

    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)

        host = parsed.hostname
        if not host:
            return False, "URL has no valid hostname"

        return validate_endpoint_host(host, settings)
    except Exception as e:
        return False, f"Invalid URL: {e}"


def validate_endpoint_host_with_settings(
    host: str | None,
    netbox_session: "Api | None" = None,
) -> tuple[bool, str]:
    """Validate endpoint host using settings fetched from NetBox.

    This is a convenience function that fetches settings and validates.
    """
    from proxbox_api.settings_client import get_settings

    settings = get_settings(netbox_session)
    return validate_endpoint_host(host, settings)
