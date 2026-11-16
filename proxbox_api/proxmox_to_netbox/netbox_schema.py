"""NetBox OpenAPI fetch and fallback schema contract helpers."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from netbox_sdk.config import authorization_header_value
from sqlmodel import select

from proxbox_api import runtime_settings
from proxbox_api.database import NetBoxEndpoint, get_session
from proxbox_api.logger import logger
from proxbox_api.session.netbox import netbox_config_from_endpoint

PERSIST_ENV = "PROXBOX_NETBOX_OPENAPI_PERSIST"
CONTRACT_TTL_ENV = "PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL"
DEFAULT_CONTRACT_TTL_SECONDS = 3600.0
# A result that did not come from a live fetch (on-disk or in-memory cache, or
# the bundled fallback) means NetBox did not answer. Keep it only briefly: long
# enough that an unreachable NetBox is not probed for every object of a sync,
# short enough that recovery is picked up quickly.
RETRY_CONTRACT_TTL_SECONDS = 60.0

# In-memory fallback store used when filesystem persistence is disabled. It lets
# schema resolution reuse a fetched document across calls without ever touching
# the filesystem (read-only deployments, "no writes to disk" operators).
_in_memory_openapi_cache: dict[str, object] | None = None


def netbox_openapi_persistence_enabled() -> bool:
    """Return True when the NetBox OpenAPI cache may be read/written on disk.

    Persistence is enabled by default. Disable it — via the
    ``PROXBOX_NETBOX_OPENAPI_PERSIST`` env var or the ``netbox_openapi_persist``
    NetBox plugin setting — to run schema resolution fully in-memory and never
    touch the filesystem (read-only filesystems, no-disk-write deployments).
    Resolution order is env override > ``ProxboxPluginSettings`` > default,
    handled by :func:`proxbox_api.runtime_settings.get_bool`.
    """

    return runtime_settings.get_bool(
        settings_key="netbox_openapi_persist",
        env=PERSIST_ENV,
        default=True,
    )


def netbox_openapi_cache_path() -> Path:
    """Return canonical path for cached NetBox OpenAPI document."""

    return Path(__file__).resolve().parents[1] / "generated" / "netbox" / "openapi.json"


def _candidate_schema_urls(base_url: str) -> list[str]:
    base = base_url.rstrip("/")
    return [
        f"{base}/api/schema/",
    ]


def _extract_netbox_endpoint_from_db() -> NetBoxEndpoint | None:
    try:
        with closing(get_session()) as session_iter:
            database_session = next(session_iter)
            return database_session.exec(select(NetBoxEndpoint)).first()
    except Exception as error:
        logger.warning("Unable to load NetBox endpoint from database: %s", error)
        return None


def fetch_live_netbox_openapi(timeout: int = 20) -> dict[str, object] | None:
    """Fetch live NetBox OpenAPI from configured endpoint using known schema URLs."""

    endpoint = _extract_netbox_endpoint_from_db()
    if endpoint is None:
        return None

    headers = {"Accept": "application/json"}
    auth = authorization_header_value(netbox_config_from_endpoint(endpoint))
    if auth:
        headers["Authorization"] = auth

    for url in _candidate_schema_urls(endpoint.url):
        try:
            request = Request(url=url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            data = json.loads(body)
            if isinstance(data, dict) and "paths" in data:
                return data
        except (URLError, TimeoutError, json.JSONDecodeError, ValueError):
            continue
        except Exception as error:
            logger.warning("Unexpected error fetching NetBox OpenAPI from %s: %s", url, error)
            continue
    return None


async def fetch_live_netbox_openapi_async(timeout: int = 20) -> dict[str, object] | None:
    """Async version: Fetch live NetBox OpenAPI from configured endpoint."""

    endpoint = _extract_netbox_endpoint_from_db()
    if endpoint is None:
        return None

    headers = {"Accept": "application/json"}
    auth = authorization_header_value(netbox_config_from_endpoint(endpoint))
    if auth:
        headers["Authorization"] = auth

    for url in _candidate_schema_urls(endpoint.url):
        try:
            request = Request(url=url, headers=headers)

            def _fetch():
                with urlopen(request, timeout=timeout) as response:
                    return response.read().decode("utf-8")

            body = await asyncio.to_thread(_fetch)
            data = json.loads(body)
            if isinstance(data, dict) and "paths" in data:
                return data
        except (URLError, TimeoutError, json.JSONDecodeError, ValueError):
            continue
        except Exception as error:
            logger.warning("Unexpected error fetching NetBox OpenAPI from %s: %s", url, error)
            continue
    return None


def save_netbox_openapi_cache(document: dict[str, object]) -> None:
    """Persist fetched NetBox OpenAPI document.

    When filesystem persistence is disabled the document is retained in an
    in-memory store instead of being written to disk, so schema resolution still
    avoids repeated live fetches while writing nothing to the filesystem.
    """

    global _in_memory_openapi_cache

    if not netbox_openapi_persistence_enabled():
        _in_memory_openapi_cache = document
        return

    path = netbox_openapi_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def load_netbox_openapi_cache() -> dict[str, object] | None:
    """Load cached NetBox OpenAPI from the in-memory store or disk.

    With persistence disabled, only the in-memory store is consulted; the
    filesystem is never read.
    """

    if not netbox_openapi_persistence_enabled():
        return _in_memory_openapi_cache

    path = netbox_openapi_cache_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Unable to load NetBox OpenAPI cache from %s: %s", path, error)
        return None


def netbox_virtual_machine_fallback_contract() -> dict[str, object]:
    """Return conservative fallback contract derived from NetBox REST docs."""

    return {
        "required_fields": ["name", "status", "cluster"],
        "optional_fields": [
            "device",
            "role",
            "vcpus",
            "memory",
            "disk",
            "tags",
            "description",
        ],
        "status_examples": ["active", "offline", "planned"],
        "endpoint": "/api/virtualization/virtual-machines/",
    }


@dataclass(frozen=True, slots=True)
class _ResolvedContract:
    """One resolved schema contract and the conditions under which it is valid."""

    contract: dict[str, object]
    expires_at: float
    endpoint_identity: tuple[object, str] | None


# Sync workers resolve the contract once per object, from several threads. The
# resolved contract is kept here so that one NetBox OpenAPI download serves the
# whole run instead of one download (about 16 MB) per object.
_resolved_contract: _ResolvedContract | None = None
_resolved_contract_lock = threading.Lock()
# Module-level alias so that tests can drive expiry without patching the clock
# that the standard library itself uses.
_monotonic = time.monotonic


def netbox_schema_contract_ttl_seconds() -> float:
    """Return how long a resolved live or cached contract is reused, in seconds.

    Set ``PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL`` to change it. ``0`` disables
    reuse, so every call resolves the contract again.
    """

    return runtime_settings.get_float(
        settings_key="netbox_schema_contract_ttl",
        env=CONTRACT_TTL_ENV,
        default=DEFAULT_CONTRACT_TTL_SECONDS,
        minimum=0.0,
        # Environment-only setting: do not query the NetBox plugin settings.
        settings_override={},
    )


def reset_netbox_schema_contract_cache() -> None:
    """Forget the resolved contract so that the next call resolves it again."""

    global _resolved_contract

    with _resolved_contract_lock:
        _resolved_contract = None


def _netbox_endpoint_identity() -> tuple[object, str] | None:
    endpoint = _extract_netbox_endpoint_from_db()
    if endpoint is None:
        return None
    return (endpoint.id, str(endpoint.url))


def _reusable_contract(
    endpoint_identity: tuple[object, str] | None,
) -> dict[str, object] | None:
    resolved = _resolved_contract
    if resolved is None:
        return None
    if resolved.endpoint_identity != endpoint_identity:
        return None
    if _monotonic() >= resolved.expires_at:
        return None
    return dict(resolved.contract)


def _contract_ttl_for_source(source: object, configured_ttl: float) -> float:
    if source == "live":
        return configured_ttl
    return min(RETRY_CONTRACT_TTL_SECONDS, configured_ttl)


def resolve_netbox_schema_contract() -> dict[str, object]:
    """Resolve NetBox schema contract from live OpenAPI, cache, or fallback docs.

    A contract resolved from a live fetch is reused for
    ``PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL`` seconds while the configured NetBox
    endpoint stays the same. A contract resolved from the cache or the fallback
    (NetBox did not answer) is reused for at most 60 seconds. Concurrent callers
    wait for one resolution instead of each downloading the OpenAPI document.

    This function can block on network and disk I/O. Call it from a worker
    thread, not from the event loop.
    """

    global _resolved_contract

    configured_ttl = netbox_schema_contract_ttl_seconds()
    if configured_ttl <= 0:
        # Reuse is disabled, so there is no result to share: do not make
        # callers queue behind each other's downloads.
        return _resolve_netbox_schema_contract_uncached()

    endpoint_identity = _netbox_endpoint_identity()
    reusable = _reusable_contract(endpoint_identity)
    if reusable is not None:
        return reusable

    with _resolved_contract_lock:
        reusable = _reusable_contract(endpoint_identity)
        if reusable is not None:
            return reusable

        contract = _resolve_netbox_schema_contract_uncached()
        _resolved_contract = _ResolvedContract(
            contract=contract,
            expires_at=_monotonic()
            + _contract_ttl_for_source(contract.get("source"), configured_ttl),
            endpoint_identity=endpoint_identity,
        )
        return dict(contract)


def _resolve_netbox_schema_contract_uncached() -> dict[str, object]:
    """Resolve the contract from live OpenAPI, cache, or fallback, with no reuse."""

    live = fetch_live_netbox_openapi()
    if live:
        save_netbox_openapi_cache(live)
        return {
            "source": "live",
            "openapi": live,
        }

    cached = load_netbox_openapi_cache()
    if cached:
        return {
            "source": "cache",
            "openapi": cached,
        }

    return {
        "source": "fallback",
        "openapi": {},
        "contract": netbox_virtual_machine_fallback_contract(),
    }


def netbox_openapi_schema_source() -> str:
    """Return human-readable source used for NetBox schema contract resolution."""

    return str(resolve_netbox_schema_contract().get("source", "unknown"))
