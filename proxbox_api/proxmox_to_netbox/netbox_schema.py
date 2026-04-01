"""NetBox OpenAPI fetch and fallback schema contract helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from netbox_sdk.config import authorization_header_value
from sqlmodel import select

from proxbox_api.database import NetBoxEndpoint, get_session
from proxbox_api.logger import logger
from proxbox_api.session.netbox import netbox_config_from_endpoint


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
        database_session = next(get_session())
        endpoint = database_session.exec(select(NetBoxEndpoint)).first()
        return endpoint
    except Exception as error:
        logger.warning("Unable to load NetBox endpoint from database: %s", error)
        return None


def fetch_live_netbox_openapi(timeout: int = 20) -> dict[str, Any] | None:
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


def save_netbox_openapi_cache(document: dict[str, Any]) -> None:
    """Persist fetched NetBox OpenAPI document to local cache."""

    path = netbox_openapi_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def load_netbox_openapi_cache() -> dict[str, Any] | None:
    """Load cached NetBox OpenAPI from disk if present."""

    path = netbox_openapi_cache_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Unable to load NetBox OpenAPI cache from %s: %s", path, error)
        return None


def netbox_virtual_machine_fallback_contract() -> dict[str, Any]:
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


def resolve_netbox_schema_contract() -> dict[str, Any]:
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
