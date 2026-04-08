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

from proxbox_api.database import DatabaseSessionDep, NetBoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_sdk_sync import SyncProxy

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
        tv = "v1"
    key = endpoint.token_key.strip() if endpoint.token_key else None
    if tv == "v1":
        key = None
    return Config(
        base_url=endpoint.url,
        token_version=tv,
        token_key=key,
        token_secret=endpoint.token,
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
    return Api(client=NetBoxApiClient(cfg), schema=build_schema_index(version="4.5"))


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


NetBoxClient = Api | SyncProxy


def get_netbox_session(database_session: DatabaseSessionDep) -> NetBoxClient:
    """
    Get NetBox API parameters from database and establish a netbox-sdk API session.
    """
    try:
        netbox_endpoint = database_session.exec(select(NetBoxEndpoint)).first()

        if not netbox_endpoint:
            raise ProxboxException(
                message="No NetBox endpoint found",
                detail="Please add a NetBox endpoint in the database",
            )

        return SyncProxy(netbox_api_from_endpoint(netbox_endpoint))

    except ProxboxException:
        raise

    except Exception as error:
        raise ProxboxException(
            message="Error establishing NetBox API session", python_exception=str(error)
        )


def get_netbox_async_session(database_session: DatabaseSessionDep) -> Api:
    """
    Get NetBox API parameters from database and establish an async netbox-sdk API session.
    """
    try:
        netbox_endpoint = database_session.exec(select(NetBoxEndpoint)).first()

        if not netbox_endpoint:
            raise ProxboxException(
                message="No NetBox endpoint found",
                detail="Please add a NetBox endpoint in the database",
            )

        return netbox_api_from_endpoint(netbox_endpoint)

    except ProxboxException:
        raise

    except Exception as error:
        raise ProxboxException(
            message="Error establishing NetBox API session", python_exception=str(error)
        )


NetBoxSessionDep = Annotated[NetBoxClient, Depends(get_netbox_session)]
NetBoxAsyncSessionDep = Annotated[Api, Depends(get_netbox_async_session)]


async def check_netbox_connection(nb: NetBoxClient) -> dict[str, object]:
    """
    Check NetBox connectivity and return status information.

    Returns:
        dict with keys: available (bool), url (str), error (str or None)
    """
    from proxbox_api.netbox_rest import rest_list_async

    try:
        api = nb
        if isinstance(nb, SyncProxy):
            api = object.__getattribute__(nb, "_obj")

        url = api.config.base_url
        await rest_list_async(nb, "/api/", query={"limit": 1})
        return {"available": True, "url": url, "error": None}
    except ProxboxException as e:
        return {
            "available": False,
            "url": getattr(getattr(nb, "config", None), "base_url", "unknown"),
            "error": e.detail or e.message,
        }
    except Exception as e:
        return {"available": False, "url": "unknown", "error": str(e)}
