"""NetBox API session creation and dependency wiring."""

from __future__ import annotations

import hashlib
import threading
from typing import Annotated

from fastapi import Depends
from netbox_sdk.client import NetBoxApiClient
from netbox_sdk.config import Config
from netbox_sdk.facade import Api
from netbox_sdk.schema import build_schema_index
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.constants import NETBOX_SCHEMA_VERSION
from proxbox_api.database import DatabaseSessionDep, NetBoxEndpoint, get_async_session
from proxbox_api.exception import ProxboxException
from proxbox_api.runtime_settings import get_float
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

_DEFAULT_NETBOX_TIMEOUT = 120.0


def _resolve_netbox_timeout() -> float:
    return get_float(
        settings_key="netbox_timeout",
        env="PROXBOX_NETBOX_TIMEOUT",
        default=_DEFAULT_NETBOX_TIMEOUT,
        minimum=1.0,
    )


def netbox_config_from_endpoint(endpoint: NetBoxEndpoint) -> Config:
    """Build netbox-sdk Config from a stored NetBox endpoint (v1 or v2 tokens)."""
    tv = (endpoint.token_version or "v1").strip().lower()
    if tv not in ("v1", "v2"):
        raise ProxboxException(
            message="Invalid token version in stored endpoint",
            detail=f"Token version must be 'v1' or 'v2', got '{tv}'",
        )
    decrypted_key = endpoint.get_decrypted_token_key()
    key = decrypted_key.strip() if decrypted_key else None
    if tv == "v1":
        key = None
    decrypted_token = endpoint.get_decrypted_token()
    return Config(
        base_url=endpoint.url,
        token_version=tv,
        token_key=key,
        token_secret=decrypted_token,
        timeout=_resolve_netbox_timeout(),
        ssl_verify=endpoint.verify_ssl,
    )


_API_CACHE_LOCK = threading.Lock()
# Keyed on (endpoint_id, config_fingerprint). The fingerprint hashes the active
# Config (URL/token/version) so token rotation produces a new key and the stale
# Api becomes unreachable; explicit invalidation drops it from memory.
_API_CACHE: dict[tuple[int, str], Api] = {}


def _config_fingerprint(cfg: Config, ssl_verify: bool) -> str:
    parts = [
        cfg.base_url or "",
        cfg.token_version or "",
        cfg.token_key or "",
        cfg.token_secret or "",
        f"{cfg.timeout:.3f}",
        "1" if ssl_verify else "0",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def invalidate_netbox_api_cache(endpoint_id: int | None = None) -> None:
    """Drop cached Api objects for an endpoint, or for all endpoints when id is None.

    Call this after updating or deleting a NetBoxEndpoint so that the next session
    request rebuilds the client with fresh credentials and no decrypted token is
    retained in memory beyond its useful life.
    """
    with _API_CACHE_LOCK:
        if endpoint_id is None:
            _API_CACHE.clear()
            return
        for key in [k for k in _API_CACHE if k[0] == endpoint_id]:
            _API_CACHE.pop(key, None)


def netbox_api_from_endpoint(endpoint: NetBoxEndpoint) -> Api:
    """Instantiate netbox-sdk Api using NetBoxApiClient + Config (no string token shortcut)."""
    cfg = netbox_config_from_endpoint(endpoint)
    fingerprint = _config_fingerprint(cfg, bool(endpoint.verify_ssl))
    cache_key = (endpoint.id or 0, fingerprint)
    with _API_CACHE_LOCK:
        cached = _API_CACHE.get(cache_key)
        if cached is not None:
            return cached
    api = Api(client=NetBoxApiClient(cfg), schema=build_schema_index(version=NETBOX_SCHEMA_VERSION))
    with _API_CACHE_LOCK:
        _API_CACHE.setdefault(cache_key, api)
        return _API_CACHE[cache_key]


def get_netbox_session(
    database_session: DatabaseSessionDep,
    netbox_id: int | None = None,
) -> Api:
    """
    Get NetBox API parameters from database and establish a netbox-sdk API session.

    Args:
        database_session: Database session dependency.
        netbox_id: Optional specific NetBox endpoint ID. If not provided, selects by
            ID when multiple endpoints exist, or returns the only endpoint when only
            one exists.

    Returns:
        NetBox API session for the endpoint.

    Raises:
        ProxboxException: If no endpoint found or on error.
    """
    try:
        if netbox_id is not None:
            netbox_endpoint = database_session.get(NetBoxEndpoint, netbox_id)
            if not netbox_endpoint:
                raise ProxboxException(
                    message=f"NetBox endpoint {netbox_id} not found",
                    detail=f"No endpoint with ID {netbox_id}",
                )
            return netbox_api_from_endpoint(netbox_endpoint)

        count = database_session.exec(select(NetBoxEndpoint)).all()
        count = len(count) if count else 0

        if count == 0:
            raise ProxboxException(
                message="No NetBox endpoint found",
                detail="Please add a NetBox endpoint in the database",
            )

        if count == 1:
            netbox_endpoint = database_session.exec(select(NetBoxEndpoint)).first()
        else:
            netbox_endpoint = database_session.exec(
                select(NetBoxEndpoint).order_by(NetBoxEndpoint.id)
            ).first()

        if not netbox_endpoint:
            raise ProxboxException(
                message="Could not resolve NetBox endpoint",
                detail="Unable to select endpoint from database",
            )

        return netbox_api_from_endpoint(netbox_endpoint)

    except ProxboxException:
        raise

    except Exception as error:
        raise ProxboxException(
            message="Error establishing NetBox API session", python_exception=str(error)
        )


async def get_netbox_async_session(
    database_session: AsyncSession = Depends(get_async_session),
    netbox_id: int | None = None,
) -> Api:
    """
    Get NetBox API parameters from database and establish an async netbox-sdk API session.

    Args:
        database_session: Database session dependency.
        netbox_id: Optional specific NetBox endpoint ID. If not provided, selects by
            ID when multiple endpoints exist, or returns the only endpoint when only
            one exists.

    Returns:
        NetBox async API session for the endpoint.

    Raises:
        ProxboxException: If no endpoint found or on error.
    """
    try:
        if netbox_id is not None:
            netbox_endpoint = await _maybe_await(database_session.get(NetBoxEndpoint, netbox_id))
            if not netbox_endpoint:
                raise ProxboxException(
                    message=f"NetBox endpoint {netbox_id} not found",
                    detail=f"No endpoint with ID {netbox_id}",
                )
            return netbox_api_from_endpoint(netbox_endpoint)

        # Fetch all endpoints to determine how many exist
        endpoints = await _maybe_await(database_session.exec(select(NetBoxEndpoint)))
        endpoints_list = endpoints.all() if endpoints else []
        count = len(endpoints_list) if endpoints_list else 0

        if count == 0:
            raise ProxboxException(
                message="No NetBox endpoint found",
                detail="Please add a NetBox endpoint in the database",
            )

        # Fetch the endpoint with a single query
        result = await _maybe_await(
            database_session.exec(select(NetBoxEndpoint).order_by(NetBoxEndpoint.id))
        )
        netbox_endpoint = result.first()

        if not netbox_endpoint:
            raise ProxboxException(
                message="Could not resolve NetBox endpoint",
                detail="Unable to select endpoint from database",
            )

        return netbox_api_from_endpoint(netbox_endpoint)

    except ProxboxException:
        raise

    except Exception as error:
        raise ProxboxException(
            message="Error establishing NetBox API session", python_exception=str(error)
        )


NetBoxSessionDep = Annotated[object, Depends(get_netbox_session)]
NetBoxAsyncSessionDep = Annotated[object, Depends(get_netbox_async_session)]


async def check_netbox_connection(nb: Api) -> dict[str, object]:
    """
    Check NetBox connectivity and return status information.

    Returns:
        dict with keys: available (bool), url (str), error (str or None)
    """
    from proxbox_api.netbox_rest import rest_list_async

    try:
        url = nb.config.base_url
        await rest_list_async(nb, "/api/", query={"limit": 1})
        return {"available": True, "url": url, "error": None}
    except ProxboxException as e:
        return {
            "available": False,
            "url": getattr(nb, "config", None)
            and getattr(nb.config, "base_url", "unknown")
            or "unknown",
            "error": e.detail or e.message,
        }
    except Exception as e:
        return {"available": False, "url": "unknown", "error": str(e)}
