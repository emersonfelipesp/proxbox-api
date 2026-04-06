"""SSRF protection utilities for validating endpoint configurations."""

from __future__ import annotations

import ipaddress
import os
import re

from proxbox_api.logger import logger

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


def _is_ssrf_protection_enabled() -> bool:
    """Check if SSRF protection is enabled via environment variable.

    Returns True if enabled (default), False if explicitly disabled.
    Logs a warning when SSRF protection is disabled.
    """
    flag = os.environ.get("PROXBOX_SSRF_PROTECTION", "true").lower()
    enabled = flag in ("true", "1", "yes")
    if not enabled:
        logger.warning(
            "SSRF protection is DISABLED. This is dangerous in production. "
            "Set PROXBOX_SSRF_PROTECTION=true to enable."
        )
    return enabled


def is_ip_blocked(ip: str) -> bool:
    """Check if an IP address is in a blocked/internal range."""
    try:
        addr = ipaddress.ip_address(ip)
        for network in INTERNAL_IP_RANGES:
            if addr in network:
                return True
    except ValueError:
        pass
    return False


def validate_endpoint_host(host: str | None) -> tuple[bool, str]:
    """Validate that an endpoint host is not an internal/reserved IP.

    Returns (is_safe, reason).

    SSRF protection can be disabled via PROXBOX_SSRF_PROTECTION=false
    environment variable (useful for development/testing with internal IPs).
    """
    if not _is_ssrf_protection_enabled():
        return True, "SSRF protection disabled"

    if not host:
        return False, "Host cannot be empty"

    host = host.strip()

    if is_ip_blocked(host):
        return False, f"Host '{host}' is a reserved/internal IP address"

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


def validate_endpoint_url(url: str | None) -> tuple[bool, str]:
    """Validate that a URL doesn't point to an internal resource."""
    if not url:
        return False, "URL cannot be empty"

    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)

        host = parsed.hostname
        if not host:
            return False, "URL has no valid hostname"

        return validate_endpoint_host(host)
    except Exception as e:
        return False, f"Invalid URL: {e}"
