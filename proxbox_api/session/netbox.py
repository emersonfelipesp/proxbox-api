"""NetBox API session creation and dependency wiring."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from typing import TYPE_CHECKING, Annotated, Any, cast

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

if TYPE_CHECKING:
    from typing import Protocol

    class _EndpointResult(Protocol):
        def all(self) -> list[NetBoxEndpoint]: ...

        def first(self) -> NetBoxEndpoint | None: ...


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
_RETIRED_APIS: list[Api] = []
_API_CACHE_OWNERS = 0


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


def _detach_cached_apis(endpoint_id: int | None) -> list[Api]:
    """Retire one endpoint or detach every client at the shutdown boundary."""
    with _API_CACHE_LOCK:
        if endpoint_id is None:
            detached = list(_RETIRED_APIS) + list(_API_CACHE.values())
            _RETIRED_APIS.clear()
            _API_CACHE.clear()
            return detached
        keys = [key for key in _API_CACHE if key[0] == endpoint_id]
        _RETIRED_APIS.extend(_API_CACHE.pop(key) for key in keys)
        return []


async def _close_cached_apis(apis: list[Api]) -> None:
    """Close every detached client and retain failures for a later retry."""
    seen: set[int] = set()
    failed: list[tuple[Api, str]] = []
    for api in apis:
        client = api.client
        identity = id(client)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            await _maybe_await(client.close())
        except Exception as error:  # noqa: BLE001
            failed.append((api, type(error).__name__))
    if failed:
        with _API_CACHE_LOCK:
            _RETIRED_APIS.extend(api for api, _error_type in failed)
        error_types = ", ".join(error_type for _api, error_type in failed)
        raise RuntimeError(f"Failed to close retired NetBox API clients: {error_types}")


async def _finish_close_despite_cancellation(apis: list[Api]) -> None:
    """Finish closure through repeated cancellation, then forward cancellation."""
    close_task = asyncio.create_task(_close_cached_apis(apis))
    cancelled = False
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    await close_task
    if cancelled:
        raise asyncio.CancelledError


async def invalidate_netbox_api_cache(endpoint_id: int | None = None) -> None:
    """Detach and close cached clients for one endpoint or the entire cache.

    Call this after updating or deleting a NetBoxEndpoint so that the next session
    request rebuilds the client with fresh credentials and no decrypted token is
    retained in memory beyond its useful life.
    """
    detached = _detach_cached_apis(endpoint_id)
    if detached:
        await _finish_close_despite_cancellation(detached)


def acquire_netbox_api_cache_owner() -> None:
    """Register one active application lifespan as a cache owner."""
    global _API_CACHE_OWNERS
    with _API_CACHE_LOCK:
        _API_CACHE_OWNERS += 1


async def release_netbox_api_cache_owner() -> None:
    """Release one lifespan owner and drain clients after the final owner exits."""
    global _API_CACHE_OWNERS
    detached: list[Api] = []
    with _API_CACHE_LOCK:
        if _API_CACHE_OWNERS <= 0:
            raise RuntimeError("NetBox API cache owner release without acquisition")
        _API_CACHE_OWNERS -= 1
        if _API_CACHE_OWNERS == 0:
            detached = list(_RETIRED_APIS) + list(_API_CACHE.values())
            _RETIRED_APIS.clear()
            _API_CACHE.clear()
    if detached:
        await _finish_close_despite_cancellation(detached)


def netbox_api_from_endpoint(endpoint: NetBoxEndpoint) -> Api:
    """Instantiate netbox-sdk Api using NetBoxApiClient + Config (no string token shortcut)."""
    cfg = netbox_config_from_endpoint(endpoint)
    fingerprint = _config_fingerprint(cfg, bool(endpoint.verify_ssl))
    cache_key = (endpoint.id or 0, fingerprint)
    with _API_CACHE_LOCK:
        cached = _API_CACHE.get(cache_key)
        if cached is not None:
            return cached
        api = Api(
            client=NetBoxApiClient(cfg),
            schema=build_schema_index(version=NETBOX_SCHEMA_VERSION),
        )
        _API_CACHE[cache_key] = api
        return api


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

        count = database_session.exec(
            select(NetBoxEndpoint).where(NetBoxEndpoint.enabled == True)  # noqa: E712
        ).all()
        count = len(count) if count else 0

        if count == 0:
            raise ProxboxException(
                message="No NetBox endpoint found",
                detail="Please add a NetBox endpoint in the database",
            )

        if count == 1:
            netbox_endpoint = database_session.exec(
                select(NetBoxEndpoint).where(NetBoxEndpoint.enabled == True)  # noqa: E712
            ).first()
        else:
            netbox_endpoint = database_session.exec(
                select(NetBoxEndpoint)
                .where(NetBoxEndpoint.enabled == True)  # noqa: E712
                .order_by(cast("Any", NetBoxEndpoint.id))
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
            netbox_endpoint = cast(
                "NetBoxEndpoint | None",
                await _maybe_await(database_session.get(NetBoxEndpoint, netbox_id)),
            )
            if not netbox_endpoint:
                raise ProxboxException(
                    message=f"NetBox endpoint {netbox_id} not found",
                    detail=f"No endpoint with ID {netbox_id}",
                )
            return netbox_api_from_endpoint(netbox_endpoint)

        # Fetch all enabled endpoints to determine how many exist
        endpoints = cast(
            "_EndpointResult | None",
            await _maybe_await(
                database_session.exec(
                    select(NetBoxEndpoint).where(NetBoxEndpoint.enabled == True)  # noqa: E712
                )
            ),
        )
        endpoints_list = endpoints.all() if endpoints else []
        count = len(endpoints_list) if endpoints_list else 0

        if count == 0:
            raise ProxboxException(
                message="No NetBox endpoint found",
                detail="Please add a NetBox endpoint in the database",
            )

        # Fetch the first enabled endpoint ordered by ID
        result = cast(
            "_EndpointResult",
            await _maybe_await(
                database_session.exec(
                    select(NetBoxEndpoint)
                    .where(NetBoxEndpoint.enabled == True)  # noqa: E712
                    .order_by(cast("Any", NetBoxEndpoint.id))
                )
            ),
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


NetBoxSessionDep = Annotated[Api, Depends(get_netbox_session)]
NetBoxAsyncSessionDep = Annotated[Api, Depends(get_netbox_async_session)]
