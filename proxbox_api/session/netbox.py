"""NetBox API session creation and dependency wiring."""

from __future__ import annotations

import os
from functools import lru_cache
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
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

_DEFAULT_NETBOX_TIMEOUT = 120.0


def _resolve_netbox_timeout() -> float:
    raw_value = os.environ.get("PROXBOX_NETBOX_TIMEOUT", "").strip()
    if not raw_value:
        return _DEFAULT_NETBOX_TIMEOUT
    try:
        timeout = float(raw_value)
    except ValueError:
        return _DEFAULT_NETBOX_TIMEOUT
    return timeout if timeout > 0 else _DEFAULT_NETBOX_TIMEOUT


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


@lru_cache(maxsize=16)
def _cached_netbox_api(
    base_url: str,
    token_version: str,
    token_key: str | None,
    token_secret: str | None,
    timeout: float,
    ssl_verify: bool,
) -> Api:
    """Build and cache a netbox-sdk Api for a stable endpoint configuration."""
    cfg = Config(
        base_url=base_url,
        token_version=token_version,
        token_key=token_key,
        token_secret=token_secret,
        timeout=timeout,
        ssl_verify=ssl_verify,
    )
    return Api(client=NetBoxApiClient(cfg), schema=build_schema_index(version=NETBOX_SCHEMA_VERSION))


def netbox_api_from_endpoint(endpoint: NetBoxEndpoint) -> Api:
    """Instantiate netbox-sdk Api using NetBoxApiClient + Config (no string token shortcut)."""
    cfg = netbox_config_from_endpoint(endpoint)
    return _cached_netbox_api(
        cfg.base_url or "",
        cfg.token_version,
        cfg.token_key,
        cfg.token_secret,
        cfg.timeout,
        bool(endpoint.verify_ssl),
    )


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
