"""SSRF protection utilities for validating endpoint configurations.

SSRF protection validates that endpoints do not point to restricted IP addresses.
Settings can be configured via NetBox plugin (ProxboxPluginSettings) with caching.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from netbox_sdk.facade import Api

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


def is_ip_blocked(  # noqa: C901
    ip: str,
    settings: dict | None = None,
) -> tuple[bool, str]:
    """Check if an IP address is blocked based on settings.

    Returns (is_blocked, reason).
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False, "OK"

    if settings is None:
        settings = {}

    ssrf_enabled = settings.get("ssrf_protection_enabled", True)
    if not ssrf_enabled:
        return False, "OK"

    blocked_ranges = settings.get("blocked_ip_ranges", [])
    for network in blocked_ranges:
        if addr in network:
            return True, f"Host '{ip}' is in explicitly blocked range {network}"

    allowed_ranges = settings.get("allowed_ip_ranges", [])
    for network in allowed_ranges:
        if addr in network:
            return False, "OK"

    allow_private = settings.get("allow_private_ips", True)
    if allow_private:
        for network in PRIVATE_IP_RANGES:
            if addr in network:
                return False, "OK"

    for network in INTERNAL_IP_RANGES:
        if addr in network:
            return True, f"Host '{ip}' is a reserved/internal IP address"

    return False, "OK"


def validate_endpoint_host(
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
    """
    if not host:
        return False, "Host cannot be empty"

    host = host.strip()

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
