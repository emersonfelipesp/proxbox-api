"""NetBox API session creation and dependency wiring."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends
from netbox_sdk.client import NetBoxApiClient
from netbox_sdk.config import Config
from netbox_sdk.facade import Api
from sqlmodel import select

from proxbox_api.database import DatabaseSessionDep, NetBoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_sdk_sync import SyncProxy


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
        ssl_verify=endpoint.verify_ssl,
    )


def netbox_api_from_endpoint(endpoint: NetBoxEndpoint) -> Api:
    """Instantiate netbox-sdk Api using NetBoxApiClient + Config (no string token shortcut)."""
    cfg = netbox_config_from_endpoint(endpoint)
    return Api(client=NetBoxApiClient(cfg))


def get_netbox_session(database_session: DatabaseSessionDep) -> Any:
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

    except ProxboxException as error:
        raise error

    except Exception as error:
        raise ProxboxException(
            message="Error establishing NetBox API session", python_exception=str(error)
        )


NetBoxSessionDep = Annotated[Any, Depends(get_netbox_session)]
