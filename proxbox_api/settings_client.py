"""Client for fetching ProxboxPluginSettings from NetBox plugin API."""

from __future__ import annotations

import ipaddress
import time
from typing import TYPE_CHECKING

from proxbox_api.logger import logger
from proxbox_api.types import ProxboxSettingsDict

if TYPE_CHECKING:
    from netbox_sdk.facade import Api

_SETTINGS_CACHE: ProxboxSettingsDict | None = None
_SETTINGS_CACHE_TIME: float = 0.0
_SETTINGS_CACHE_TTL: float = 300.0  # 5 minutes


def parse_cidr_list(text: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse newline-separated CIDR ranges into a list of IPNetwork objects."""
    if not text:
        return []
    networks = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            networks.append(ipaddress.ip_network(line, strict=False))
        except ValueError:
            logger.warning("Invalid CIDR range in settings: %s", line)
    return networks


def get_default_settings() -> ProxboxSettingsDict:
    """Return default settings when NetBox is unavailable."""
    return {
        "ssrf_protection_enabled": True,
        "allow_private_ips": True,  # Permissive for on-premises
        "allowed_ip_ranges": [],
        "blocked_ip_ranges": [],
    }


def fetch_settings_from_netbox(netbox_session: "Api") -> ProxboxSettingsDict | None:
    """Fetch ProxboxPluginSettings from NetBox plugin API.

    Returns None if fetch fails.
    """
    try:
        response = netbox_session.http_session.get(
            "/api/plugins/proxbox/settings/",
            timeout=10,
        )
        if response.status_code != 200:
            logger.warning(
                "Failed to fetch ProxboxPluginSettings: HTTP %s",
                response.status_code,
            )
            return None

        data = response.json()
        if isinstance(data, list) and len(data) > 0:
            settings = data[0]
        elif isinstance(data, dict):
            settings = data
        else:
            logger.warning("Unexpected ProxboxPluginSettings response format")
            return None

        return {
            "ssrf_protection_enabled": settings.get("ssrf_protection_enabled", True),
            "allow_private_ips": settings.get("allow_private_ips", True),
            "allowed_ip_ranges": parse_cidr_list(settings.get("additional_allowed_ip_ranges", "")),
            "blocked_ip_ranges": parse_cidr_list(settings.get("explicitly_blocked_ip_ranges", "")),
        }

    except Exception as exc:
        logger.warning("Error fetching ProxboxPluginSettings: %s", exc)
        return None


def get_settings(
    netbox_session: "Api | None" = None, use_cache: bool = True
) -> ProxboxSettingsDict:
    """Get ProxboxPluginSettings with caching.

    Falls back to defaults if NetBox is unavailable.
    Uses a 5-minute cache TTL.
    """
    global _SETTINGS_CACHE, _SETTINGS_CACHE_TIME

    now = time.time()

    if use_cache and _SETTINGS_CACHE is not None:
        if now - _SETTINGS_CACHE_TIME < _SETTINGS_CACHE_TTL:
            return _SETTINGS_CACHE

    if netbox_session is None:
        from proxbox_api.app.netbox_session import get_raw_netbox_session

        try:
            netbox_session = get_raw_netbox_session()
            if netbox_session is None:
                return get_default_settings()
        except Exception as exc:
            logger.debug("Could not get NetBox session for settings: %s", exc)
            return get_default_settings()

    settings = fetch_settings_from_netbox(netbox_session)
    if settings is None:
        return get_default_settings()

    _SETTINGS_CACHE = settings
    _SETTINGS_CACHE_TIME = now
    return settings


def invalidate_settings_cache() -> None:
    """Invalidate the settings cache to force a fresh fetch."""
    global _SETTINGS_CACHE, _SETTINGS_CACHE_TIME
    _SETTINGS_CACHE = None
    _SETTINGS_CACHE_TIME = 0.0
