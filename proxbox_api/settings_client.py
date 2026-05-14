"""Client for fetching ProxboxPluginSettings from NetBox plugin API."""

from __future__ import annotations

import ipaddress
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from netbox_sdk.config import authorization_header_value

from proxbox_api.constants import DEFAULT_LOG_PATH
from proxbox_api.logger import logger
from proxbox_api.types import ProxboxSettingsDict

if TYPE_CHECKING:
    from netbox_sdk.facade import Api

_SETTINGS_CACHE: ProxboxSettingsDict | None = None
_SETTINGS_CACHE_TIME: float = 0.0
_SETTINGS_CACHE_TTL: float = 300.0  # 5 minutes
_FETCHING_SETTINGS: bool = False  # reentrance guard against credential-decryption recursion


def _coerce_role_id(value: object) -> int | None:
    """Coerce a NetBox FK field response into an int id, or None.

    The plugin REST serializer renders ForeignKey fields as nested objects (e.g.
    ``{"id": 5, ...}``), and the runtime endpoint may collapse them to raw ints
    or omit them entirely. None / empty values resolve to None.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        raw = value.get("id")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        "netbox_timeout": 120,
        "netbox_max_concurrent": 1,
        "netbox_max_retries": 5,
        "netbox_retry_delay": 2.0,
        "netbox_get_cache_ttl": 60.0,
        "netbox_get_cache_max_entries": 4096,
        "netbox_get_cache_max_bytes": 52_428_800,
        "netbox_write_concurrency": 8,
        "proxmox_fetch_concurrency": 8,
        "backup_batch_size": 5,
        "backup_batch_delay_ms": 200,
        "bulk_batch_size": 50,
        "bulk_batch_delay_ms": 500,
        "vm_sync_max_concurrency": 4,
        "custom_fields_request_delay": 0.0,
        "ensure_netbox_objects": True,
        "delete_orphans": False,
        "debug_cache": False,
        "expose_internal_errors": False,
        "proxmox_timeout": 5,
        "proxmox_max_retries": 0,
        "proxmox_retry_backoff": 0.5,
        "default_role_qemu_id": None,
        "default_role_lxc_id": None,
        "hardware_discovery_enabled": False,
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


def _extract_settings_payload(data: object) -> dict[str, object] | None:
    """Normalize supported NetBox settings API response shapes."""
    if isinstance(data, list):
        first = data[0] if data else None
        return first if isinstance(first, dict) else None

    if not isinstance(data, dict):
        return None

    if "results" in data:
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return None
        first = results[0]
        return first if isinstance(first, dict) else None

    return data


def _request_settings_json(
    *,
    base_url: str,
    path: str,
    auth: str,
    ssl_verify: bool | None,
) -> tuple[object | None, int | None]:
    url = f"{base_url}{path}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": auth, "Accept": "application/json"},
    )
    urlopen_kwargs: dict[str, object] = {"timeout": 10}
    if ssl_verify is False and urllib.parse.urlsplit(base_url).scheme.lower() == "https":
        urlopen_kwargs["context"] = ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(req, **urlopen_kwargs) as resp:
            if resp.status != 200:
                return None, resp.status
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as exc:
        return None, exc.code
    except urllib.error.URLError as exc:
        logger.warning("Error fetching ProxboxPluginSettings from %s: %s", url, exc)
        return None, None
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON fetching ProxboxPluginSettings from %s: %s", url, exc)
        return None, None


def fetch_settings_from_netbox(netbox_session: "Api") -> ProxboxSettingsDict | None:
    """Fetch ProxboxPluginSettings from NetBox plugin API.

    Returns None if fetch fails.
    """
    try:
        config = netbox_session.client.config
        base_url = (config.base_url or "").rstrip("/")

        if not base_url:
            logger.warning("NetBox base_url not configured")
            return None

        auth = authorization_header_value(config)
        if not auth:
            logger.warning("NetBox auth header could not be built — token not configured")
            return None

        settings = None
        for path in (
            "/api/plugins/proxbox/settings/runtime/",
            "/api/plugins/proxbox/settings/",
        ):
            data, status = _request_settings_json(
                base_url=base_url,
                path=path,
                auth=auth,
                ssl_verify=getattr(config, "ssl_verify", None),
            )
            if status is not None and status != 200:
                if path.endswith("/runtime/") and status == 404:
                    logger.debug("ProxboxPluginSettings runtime endpoint is not available")
                else:
                    logger.warning(
                        "Failed to fetch ProxboxPluginSettings from %s: HTTP %s",
                        path,
                        status,
                    )
                continue
            settings = _extract_settings_payload(data)
            if settings is not None:
                break

        if settings is None:
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
            "netbox_timeout": int(settings.get("netbox_timeout", 120)),
            "netbox_max_concurrent": int(settings.get("netbox_max_concurrent", 1)),
            "netbox_max_retries": int(settings.get("netbox_max_retries", 5)),
            "netbox_retry_delay": float(settings.get("netbox_retry_delay", 2.0)),
            "netbox_get_cache_ttl": float(settings.get("netbox_get_cache_ttl", 60.0)),
            "netbox_get_cache_max_entries": int(settings.get("netbox_get_cache_max_entries", 4096)),
            "netbox_get_cache_max_bytes": int(
                settings.get("netbox_get_cache_max_bytes", 52_428_800)
            ),
            "netbox_write_concurrency": int(settings.get("netbox_write_concurrency", 8)),
            "proxmox_fetch_concurrency": int(settings.get("proxmox_fetch_concurrency", 8)),
            "backup_batch_size": int(settings.get("backup_batch_size", 5)),
            "backup_batch_delay_ms": int(settings.get("backup_batch_delay_ms", 200)),
            "bulk_batch_size": int(settings.get("bulk_batch_size", 50)),
            "bulk_batch_delay_ms": int(settings.get("bulk_batch_delay_ms", 500)),
            "vm_sync_max_concurrency": int(settings.get("vm_sync_max_concurrency", 4)),
            "custom_fields_request_delay": float(settings.get("custom_fields_request_delay", 0.0)),
            "ensure_netbox_objects": bool(settings.get("ensure_netbox_objects", True)),
            "delete_orphans": bool(settings.get("delete_orphans", False)),
            "debug_cache": bool(settings.get("debug_cache", False)),
            "expose_internal_errors": bool(settings.get("expose_internal_errors", False)),
            "proxmox_timeout": int(settings.get("proxmox_timeout", 5)),
            "proxmox_max_retries": int(settings.get("proxmox_max_retries", 0)),
            "proxmox_retry_backoff": float(settings.get("proxmox_retry_backoff", 0.5)),
            "default_role_qemu_id": _coerce_role_id(settings.get("default_role_qemu")),
            "default_role_lxc_id": _coerce_role_id(settings.get("default_role_lxc")),
            "hardware_discovery_enabled": bool(settings.get("hardware_discovery_enabled", False)),
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
