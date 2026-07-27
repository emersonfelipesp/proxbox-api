"""Client for fetching ProxboxPluginSettings from NetBox plugin API."""

from __future__ import annotations

import ipaddress
import json
import math
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from netbox_sdk.config import authorization_header_value

from proxbox_api.constants import DEFAULT_LOG_PATH
from proxbox_api.logger import logger
from proxbox_api.types import ProxboxSettingsDict

if TYPE_CHECKING:
    from netbox_sdk.facade import Api

_SETTINGS_CACHE: ProxboxSettingsDict | None = None
_SETTINGS_CACHE_TIME: float = 0.0
_SETTINGS_CACHE_TTL: float = 300.0  # 5 minutes
_SETTINGS_CONDITION = threading.Condition()
_SETTINGS_FETCH_IN_PROGRESS = False
_SETTINGS_FETCH_GENERATION = 0
_SETTINGS_LAST_RESULT: ProxboxSettingsDict | None = None
_SETTINGS_THREAD_LOCAL = threading.local()
_VALID_RECONCILIATION_ENGINES = {"python", "compare", "rust"}
_DEFAULT_SETTINGS_REQUEST_TIMEOUT_SECONDS = 10.0


@contextmanager
def override_settings_for_current_thread(
    settings: ProxboxSettingsDict,
) -> Iterator[None]:
    """Provide recursion-safe settings while parsing credentials in one thread."""

    sentinel = object()
    previous = getattr(_SETTINGS_THREAD_LOCAL, "override", sentinel)
    _SETTINGS_THREAD_LOCAL.override = settings
    try:
        yield
    finally:
        if previous is sentinel:
            delattr(_SETTINGS_THREAD_LOCAL, "override")
        else:
            _SETTINGS_THREAD_LOCAL.override = previous


def _coerce_role_id(value: object) -> int | None:
    """Coerce a NetBox FK field response into an int id, or None.

    The plugin REST serializer renders ForeignKey fields as nested objects (e.g.
    ``{"id": 5, ...}``), and the runtime endpoint may collapse them to raw ints
    or omit them entirely. None / empty values resolve to None.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        raw = cast(dict[str, object], value).get("id")
        if raw is None:
            return None
        return _coerce_int(raw, default=None)
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_cidr_list(text: object) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse newline-separated CIDR ranges into a list of IPNetwork objects."""
    if not isinstance(text, str) or not text:
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
        "reconciliation_engine": "python",
        "reconciliation_compare_strict": False,
        "custom_fields_request_delay": 0.0,
        "custom_fields_enabled": False,
        "ensure_netbox_objects": True,
        "delete_orphans": False,
        "debug_cache": False,
        "expose_internal_errors": False,
        "netbox_openapi_persist": True,
        "proxmox_timeout": 5,
        "proxmox_max_retries": 0,
        "proxmox_retry_backoff": 0.5,
        "default_role_qemu_id": None,
        "default_role_lxc_id": None,
        "hardware_discovery_enabled": False,
        "cloud_network_lock_enabled": False,
        "cloud_customer_prefix_id": None,
        "cloud_customer_bridge": "",
        "cloud_customer_vlan_tag": None,
        "cloud_customer_gateway": "",
        "ceph_task_timeout": 300.0,
        "ceph_task_poll_interval": 1.0,
        "ceph_run_lease_seconds": 360.0,
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


def _normalize_reconciliation_engine(value: object) -> str:
    if not isinstance(value, str):
        return "python"
    engine = value.strip().lower()
    return engine if engine in _VALID_RECONCILIATION_ENGINES else "python"


def _coerce_bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_int(value: object, *, default: int | None) -> int | None:
    """Parse one JSON integer setting without accepting booleans."""

    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_finite_float(value: object, *, default: float) -> float:
    """Parse one JSON numeric setting without accepting booleans or NaN/Inf."""

    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if math.isfinite(parsed) else default


def _extract_settings_payload(data: object) -> dict[str, object] | None:
    """Normalize supported NetBox settings API response shapes."""
    if isinstance(data, list):
        first = data[0] if data else None
        return cast(dict[str, object], first) if isinstance(first, dict) else None

    if not isinstance(data, dict):
        return None

    payload = cast(dict[str, object], data)
    if "results" in payload:
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            return None
        first = results[0]
        return cast(dict[str, object], first) if isinstance(first, dict) else None

    return payload


def _request_settings_json(
    *,
    base_url: str,
    path: str,
    auth: str,
    ssl_verify: bool | None,
    request_timeout_seconds: float,
) -> tuple[object | None, int | None]:
    url = f"{base_url}{path}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": auth, "Accept": "application/json"},
    )
    context: ssl.SSLContext | None = None
    if ssl_verify is False and urllib.parse.urlsplit(base_url).scheme.lower() == "https":
        context = ssl._create_unverified_context()

    try:
        if context is None:
            response = urllib.request.urlopen(
                req,
                timeout=request_timeout_seconds,
            )
        else:
            response = urllib.request.urlopen(
                req,
                timeout=request_timeout_seconds,
                context=context,
            )
        with response as resp:
            if resp.status != 200:
                return None, resp.status
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as exc:
        return None, exc.code
    except urllib.error.URLError as exc:
        logger.warning("Error fetching ProxboxPluginSettings from %s: %s", url, exc)
        return None, None
    except TimeoutError as exc:
        logger.warning("Timed out fetching ProxboxPluginSettings from %s: %s", url, exc)
        return None, None
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON fetching ProxboxPluginSettings from %s: %s", url, exc)
        return None, None


def fetch_settings_from_netbox(  # noqa: C901
    netbox_session: "Api",
    *,
    request_timeout_seconds: float = _DEFAULT_SETTINGS_REQUEST_TIMEOUT_SECONDS,
) -> ProxboxSettingsDict | None:
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
        deadline = time.monotonic() + max(request_timeout_seconds, 0.0)
        for path in (
            "/api/plugins/proxbox/settings/runtime/",
            "/api/plugins/proxbox/settings/",
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Timed out fetching ProxboxPluginSettings")
                break
            data, status = _request_settings_json(
                base_url=base_url,
                path=path,
                auth=auth,
                ssl_verify=getattr(config, "ssl_verify", None),
                request_timeout_seconds=remaining,
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

        backup_batch_delay_ms = _coerce_int(settings.get("backup_batch_delay_ms"), default=None)
        if backup_batch_delay_ms is None:
            backup_batch_delay_ms = 200
        bulk_batch_delay_ms = _coerce_int(settings.get("bulk_batch_delay_ms"), default=None)
        if bulk_batch_delay_ms is None:
            bulk_batch_delay_ms = 500

        return {
            "backend_log_file_path": normalize_backend_log_file_path(
                settings.get("backend_log_file_path")
            ),
            "ssrf_protection_enabled": _coerce_bool(
                settings.get("ssrf_protection_enabled"), default=True
            ),
            "allow_private_ips": _coerce_bool(settings.get("allow_private_ips"), default=True),
            "allowed_ip_ranges": parse_cidr_list(settings.get("additional_allowed_ip_ranges", "")),
            "blocked_ip_ranges": parse_cidr_list(settings.get("explicitly_blocked_ip_ranges", "")),
            "encryption_key": str(settings.get("encryption_key", "")).strip(),
            "use_guest_agent_interface_name": _coerce_bool(
                settings.get("use_guest_agent_interface_name"), default=True
            ),
            "proxbox_fetch_max_concurrency": _coerce_int(
                settings.get("proxbox_fetch_max_concurrency"), default=8
            )
            or 8,
            "ignore_ipv6_link_local_addresses": _coerce_bool(
                settings.get("ignore_ipv6_link_local_addresses"), default=True
            ),
            "primary_ip_preference": (
                "ipv6"
                if str(settings.get("primary_ip_preference", "ipv4")).strip().lower() == "ipv6"
                else "ipv4"
            ),
            "netbox_timeout": _coerce_int(settings.get("netbox_timeout"), default=120) or 120,
            "netbox_max_concurrent": _coerce_int(settings.get("netbox_max_concurrent"), default=1)
            or 1,
            "netbox_max_retries": _coerce_int(settings.get("netbox_max_retries"), default=5) or 0,
            "netbox_retry_delay": _coerce_finite_float(
                settings.get("netbox_retry_delay"), default=2.0
            ),
            "netbox_get_cache_ttl": _coerce_finite_float(
                settings.get("netbox_get_cache_ttl"), default=60.0
            ),
            "netbox_get_cache_max_entries": _coerce_int(
                settings.get("netbox_get_cache_max_entries"), default=4096
            )
            or 4096,
            "netbox_get_cache_max_bytes": _coerce_int(
                settings.get("netbox_get_cache_max_bytes"), default=52_428_800
            )
            or 52_428_800,
            "netbox_write_concurrency": _coerce_int(
                settings.get("netbox_write_concurrency"), default=8
            )
            or 8,
            "proxmox_fetch_concurrency": _coerce_int(
                settings.get("proxmox_fetch_concurrency"), default=8
            )
            or 8,
            "backup_batch_size": _coerce_int(settings.get("backup_batch_size"), default=5) or 5,
            "backup_batch_delay_ms": backup_batch_delay_ms,
            "bulk_batch_size": _coerce_int(settings.get("bulk_batch_size"), default=50) or 50,
            "bulk_batch_delay_ms": bulk_batch_delay_ms,
            "vm_sync_max_concurrency": _coerce_int(
                settings.get("vm_sync_max_concurrency"), default=4
            )
            or 4,
            "reconciliation_engine": _normalize_reconciliation_engine(
                settings.get("reconciliation_engine")
            ),
            "reconciliation_compare_strict": _coerce_bool(
                settings.get("reconciliation_compare_strict"),
                default=False,
            ),
            "custom_fields_request_delay": _coerce_finite_float(
                settings.get("custom_fields_request_delay"), default=0.0
            ),
            "custom_fields_enabled": _coerce_bool(
                settings.get("custom_fields_enabled"),
                default=False,
            ),
            "ensure_netbox_objects": _coerce_bool(
                settings.get("ensure_netbox_objects"), default=True
            ),
            "delete_orphans": _coerce_bool(settings.get("delete_orphans"), default=False),
            "debug_cache": _coerce_bool(settings.get("debug_cache"), default=False),
            "expose_internal_errors": _coerce_bool(
                settings.get("expose_internal_errors"), default=False
            ),
            "netbox_openapi_persist": _coerce_bool(
                settings.get("netbox_openapi_persist"), default=True
            ),
            "proxmox_timeout": _coerce_int(settings.get("proxmox_timeout"), default=5) or 5,
            "proxmox_max_retries": _coerce_int(settings.get("proxmox_max_retries"), default=0) or 0,
            "proxmox_retry_backoff": _coerce_finite_float(
                settings.get("proxmox_retry_backoff"), default=0.5
            ),
            "default_role_qemu_id": _coerce_role_id(settings.get("default_role_qemu")),
            "default_role_lxc_id": _coerce_role_id(settings.get("default_role_lxc")),
            "hardware_discovery_enabled": _coerce_bool(
                settings.get("hardware_discovery_enabled"), default=False
            ),
            "cloud_network_lock_enabled": _coerce_bool(
                settings.get("cloud_network_lock_enabled"),
                default=False,
            ),
            "cloud_customer_prefix_id": _coerce_role_id(settings.get("cloud_customer_prefix_id")),
            "cloud_customer_bridge": str(settings.get("cloud_customer_bridge", "")).strip(),
            "cloud_customer_vlan_tag": _coerce_role_id(settings.get("cloud_customer_vlan_tag")),
            "cloud_customer_gateway": str(settings.get("cloud_customer_gateway", "")).strip(),
            "ceph_task_timeout": _coerce_finite_float(
                settings.get("ceph_task_timeout"), default=300.0
            ),
            "ceph_task_poll_interval": _coerce_finite_float(
                settings.get("ceph_task_poll_interval"), default=1.0
            ),
            "ceph_run_lease_seconds": _coerce_finite_float(
                settings.get("ceph_run_lease_seconds"), default=360.0
            ),
        }

    except urllib.error.URLError as exc:
        logger.warning("Error fetching ProxboxPluginSettings: %s", exc)
        return None
    except Exception as exc:
        logger.warning("Error fetching ProxboxPluginSettings: %s", exc)
        return None


def get_settings(  # noqa: C901
    netbox_session: "Api | None" = None,
    use_cache: bool = True,
    *,
    request_timeout_seconds: float | None = None,
    cache_fallback: bool = True,
) -> ProxboxSettingsDict:
    """Get ProxboxPluginSettings with caching.

    Falls back to defaults if NetBox is unavailable.
    Uses a 5-minute cache TTL.
    """
    global _SETTINGS_CACHE, _SETTINGS_CACHE_TIME
    global _SETTINGS_FETCH_IN_PROGRESS, _SETTINGS_FETCH_GENERATION, _SETTINGS_LAST_RESULT

    override = getattr(_SETTINGS_THREAD_LOCAL, "override", None)
    if override is not None:
        return override

    recursion_depth = getattr(_SETTINGS_THREAD_LOCAL, "fetch_depth", 0)
    if recursion_depth:
        # Building the NetBox facade can decrypt its token, which asks for the
        # plugin encryption key. The same-thread recursion must not deadlock on
        # the single-flight condition.
        return get_default_settings()

    deadline = (
        None
        if request_timeout_seconds is None
        else time.monotonic() + max(request_timeout_seconds, 0.0)
    )
    with _SETTINGS_CONDITION:
        while True:
            now = time.time()
            if use_cache and _SETTINGS_CACHE is not None:
                if now - _SETTINGS_CACHE_TIME < _SETTINGS_CACHE_TTL:
                    return _SETTINGS_CACHE

            if not _SETTINGS_FETCH_IN_PROGRESS:
                _SETTINGS_FETCH_IN_PROGRESS = True
                break

            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                # A bounded availability-preserving caller must never inherit an
                # unrelated, longer settings lookup already in progress.
                return get_default_settings()
            _SETTINGS_CONDITION.wait(timeout=remaining)

    _SETTINGS_THREAD_LOCAL.fetch_depth = recursion_depth + 1
    settings: ProxboxSettingsDict | None = None
    fetched: ProxboxSettingsDict | None = None
    try:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            fetched = None
        elif netbox_session is None:
            from proxbox_api.app.netbox_session import get_raw_netbox_session

            try:
                netbox_session = cast("Api | None", get_raw_netbox_session())
            except Exception as exc:
                logger.debug("Could not get NetBox session for settings: %s", exc)

        if netbox_session is None:
            fetched = None
        elif request_timeout_seconds is None:
            fetched = fetch_settings_from_netbox(netbox_session)
        else:
            remaining = deadline - time.monotonic() if deadline is not None else 0.0
            if remaining > 0:
                fetched = fetch_settings_from_netbox(
                    netbox_session,
                    request_timeout_seconds=remaining,
                )
        settings = fetched if fetched is not None else get_default_settings()
    finally:
        if recursion_depth:
            _SETTINGS_THREAD_LOCAL.fetch_depth = recursion_depth
        else:
            delattr(_SETTINGS_THREAD_LOCAL, "fetch_depth")
        with _SETTINGS_CONDITION:
            if settings is not None:
                _SETTINGS_FETCH_GENERATION += 1
                _SETTINGS_LAST_RESULT = settings
                if fetched is not None or cache_fallback:
                    _SETTINGS_CACHE = settings
                    _SETTINGS_CACHE_TIME = time.time()
            _SETTINGS_FETCH_IN_PROGRESS = False
            _SETTINGS_CONDITION.notify_all()

    if settings is None:  # pragma: no cover - the fetch path either returns or raises
        return get_default_settings()
    return settings


def invalidate_settings_cache() -> None:
    """Invalidate the settings cache to force a fresh fetch."""
    global _SETTINGS_CACHE, _SETTINGS_CACHE_TIME, _SETTINGS_LAST_RESULT
    with _SETTINGS_CONDITION:
        _SETTINGS_CACHE = None
        _SETTINGS_CACHE_TIME = 0.0
        _SETTINGS_LAST_RESULT = None
