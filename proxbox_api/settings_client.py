"""Client for fetching ProxboxPluginSettings from NetBox plugin API."""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.request
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from proxbox_api.constants import DEFAULT_LOG_PATH
from proxbox_api.logger import logger
from proxbox_api.types import ProxboxSettingsDict

if TYPE_CHECKING:
    from netbox_sdk.facade import Api

_SETTINGS_CACHE: ProxboxSettingsDict | None = None
_SETTINGS_CACHE_TIME: float = 0.0
_SETTINGS_CACHE_TTL: float = 300.0  # 5 minutes
_FETCHING_SETTINGS: bool = False  # reentrance guard against credential-decryption recursion


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
        "backend_log_file_path": DEFAULT_LOG_PATH,
        "ssrf_protection_enabled": True,
        "allow_private_ips": True,  # Permissive for on-premises
        "allowed_ip_ranges": [],
        "blocked_ip_ranges": [],
        "encryption_key": "",  # Empty string means no key in settings, use env var
        "use_guest_agent_interface_name": True,
        "proxbox_fetch_max_concurrency": 8,
        "ignore_ipv6_link_local_addresses": True,
        "primary_ip_preference": "ipv4",
        "netbox_max_concurrent": 1,
        "netbox_max_retries": 5,
        "netbox_retry_delay": 2.0,
        "netbox_get_cache_ttl": 60.0,
        "bulk_batch_size": 50,
        "bulk_batch_delay_ms": 500,
        "vm_sync_max_concurrency": 4,
        "custom_fields_request_delay": 0.0,
    }


def normalize_backend_log_file_path(value: object) -> str:
    """Return a safe absolute backend log file path."""
    if not isinstance(value, str):
        return DEFAULT_LOG_PATH
    cleaned = value.strip()
    if not cleaned:
        return DEFAULT_LOG_PATH
    if not PurePosixPath(cleaned).is_absolute():
        return DEFAULT_LOG_PATH
    if cleaned.endswith("/"):
        return DEFAULT_LOG_PATH
    return cleaned


def fetch_settings_from_netbox(netbox_session: "Api") -> ProxboxSettingsDict | None:
    """Fetch ProxboxPluginSettings from NetBox plugin API.

    Returns None if fetch fails.
    """
    try:
        config = netbox_session.client.config
        base_url = (config.base_url or "").rstrip("/")
        token_secret = config.token_secret or ""
        token_version = config.token_version or "v1"

        if not base_url:
            logger.warning("NetBox base_url not configured")
            return None

        # Build auth header (v1: "Token <secret>", v2: "nbt <key>:<secret>")
        if token_version == "v2" and config.token_key:
            auth = f"nbt {config.token_key}:{token_secret}"
        else:
            auth = f"Token {token_secret}"

        url = f"{base_url}/api/plugins/proxbox/settings/"
        req = urllib.request.Request(
            url,
            headers={"Authorization": auth, "Accept": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    "Failed to fetch ProxboxPluginSettings: HTTP %s",
                    resp.status,
                )
                return None

            data = json.loads(resp.read().decode())

        if isinstance(data, list) and len(data) > 0:
            settings = data[0]
        elif isinstance(data, dict):
            settings = data
        else:
            logger.warning("Unexpected ProxboxPluginSettings response format")
            return None

        return {
            "backend_log_file_path": normalize_backend_log_file_path(
                settings.get("backend_log_file_path")
            ),
            "ssrf_protection_enabled": settings.get("ssrf_protection_enabled", True),
            "allow_private_ips": settings.get("allow_private_ips", True),
            "allowed_ip_ranges": parse_cidr_list(settings.get("additional_allowed_ip_ranges", "")),
            "blocked_ip_ranges": parse_cidr_list(settings.get("explicitly_blocked_ip_ranges", "")),
            "encryption_key": str(settings.get("encryption_key", "")).strip(),
            "use_guest_agent_interface_name": settings.get("use_guest_agent_interface_name", True),
            "proxbox_fetch_max_concurrency": int(settings.get("proxbox_fetch_max_concurrency", 8)),
            "ignore_ipv6_link_local_addresses": settings.get(
                "ignore_ipv6_link_local_addresses", True
            ),
            "primary_ip_preference": (
                "ipv6"
                if str(settings.get("primary_ip_preference", "ipv4")).strip().lower() == "ipv6"
                else "ipv4"
            ),
            "netbox_max_concurrent": int(settings.get("netbox_max_concurrent", 1)),
            "netbox_max_retries": int(settings.get("netbox_max_retries", 5)),
            "netbox_retry_delay": float(settings.get("netbox_retry_delay", 2.0)),
            "netbox_get_cache_ttl": float(settings.get("netbox_get_cache_ttl", 60.0)),
            "bulk_batch_size": int(settings.get("bulk_batch_size", 50)),
            "bulk_batch_delay_ms": int(settings.get("bulk_batch_delay_ms", 500)),
            "vm_sync_max_concurrency": int(settings.get("vm_sync_max_concurrency", 4)),
            "custom_fields_request_delay": float(settings.get("custom_fields_request_delay", 0.0)),
        }

    except urllib.error.URLError as exc:
        logger.warning("Error fetching ProxboxPluginSettings: %s", exc)
        return None
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
    global _SETTINGS_CACHE, _SETTINGS_CACHE_TIME, _FETCHING_SETTINGS

    # Break the circular dependency: credential decryption calls get_settings()
    # to read the encryption key, which calls get_raw_netbox_session(), which
    # calls decrypt_value(), which calls get_settings() again indefinitely.
    if _FETCHING_SETTINGS:
        return get_default_settings()

    now = time.time()

    if use_cache and _SETTINGS_CACHE is not None:
        if now - _SETTINGS_CACHE_TIME < _SETTINGS_CACHE_TTL:
            return _SETTINGS_CACHE

    _FETCHING_SETTINGS = True
    try:
        if netbox_session is None:
            from proxbox_api.app.netbox_session import get_raw_netbox_session

            try:
                netbox_session = get_raw_netbox_session()
            except Exception as exc:
                logger.debug("Could not get NetBox session for settings: %s", exc)

        fetched = fetch_settings_from_netbox(netbox_session) if netbox_session is not None else None
        settings = fetched if fetched is not None else get_default_settings()
    finally:
        _FETCHING_SETTINGS = False

    _SETTINGS_CACHE = settings
    _SETTINGS_CACHE_TIME = now
    return settings


def invalidate_settings_cache() -> None:
    """Invalidate the settings cache to force a fresh fetch."""
    global _SETTINGS_CACHE, _SETTINGS_CACHE_TIME
    _SETTINGS_CACHE = None
    _SETTINGS_CACHE_TIME = 0.0
