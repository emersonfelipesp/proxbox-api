"""NetBox OpenAPI fetch and fallback schema contract helpers."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import closing
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from netbox_sdk.config import authorization_header_value
from sqlmodel import select

from proxbox_api.database import NetBoxEndpoint, get_session
from proxbox_api.logger import logger
from proxbox_api.session.netbox import netbox_config_from_endpoint

PERSIST_ENV = "PROXBOX_NETBOX_OPENAPI_PERSIST"
_FALSEY = {"0", "false", "no", "off"}

# In-memory fallback store used when filesystem persistence is disabled. It lets
# schema resolution reuse a fetched document across calls without ever touching
# the filesystem (read-only deployments, "no writes to disk" operators).
_in_memory_openapi_cache: dict[str, object] | None = None


def netbox_openapi_persistence_enabled() -> bool:
    """Return True when the NetBox OpenAPI cache may be read/written on disk.

    Persistence is enabled by default. Set ``PROXBOX_NETBOX_OPENAPI_PERSIST`` to
    a falsey value (``0``/``false``/``no``/``off``) to run schema resolution
    fully in-memory and never touch the filesystem. This is an operator-level
    deployment concern (like ``PROXBOX_GENERATED_DIR``/``PROXBOX_DATABASE_PATH``),
    so it is a process env var rather than a plugin setting.
    """

    return os.environ.get(PERSIST_ENV, "").strip().lower() not in _FALSEY


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
            "custom_fields",
            "description",
        ],
        "status_examples": ["active", "offline", "planned"],
        "endpoint": "/api/virtualization/virtual-machines/",
    }


def resolve_netbox_schema_contract() -> dict[str, object]:
    """Resolve NetBox schema contract from live OpenAPI, cache, or fallback docs."""

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
