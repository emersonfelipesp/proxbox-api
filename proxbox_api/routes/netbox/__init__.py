"""NetBox route handlers for endpoint and status operations."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import NetBoxEndpoint
from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.exception import ProxboxException
from proxbox_api.settings_client import get_settings
from proxbox_api.ssrf import clear_endpoint_cache, pre_allow_endpoint_hosts, validate_endpoint_host
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()


class NetBoxEndpointCreate(BaseModel):
    name: str = Field(max_length=255)
    ip_address: str = Field(max_length=45)
    domain: str = Field(default="", max_length=255)
    port: int = Field(default=443, ge=1, le=65535)
    token_version: str = Field(default="v1", max_length=2)
    token_key: str | None = Field(default=None, max_length=1000)
    token: str = Field(max_length=1000)
    verify_ssl: bool = True


class NetBoxEndpointUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=45)
    domain: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    token_version: str | None = Field(default=None, max_length=2)
    token_key: str | None = Field(default=None, max_length=1000)
    token: str | None = Field(default=None, max_length=1000)
    verify_ssl: bool | None = None


class NetBoxEndpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    name: str
    ip_address: str
    domain: str
    port: int
    token_version: str
    verify_ssl: bool


def _normalize_netbox_endpoint_fields(nb: NetBoxEndpoint) -> None:
    nb.token_version = (nb.token_version or "v1").strip().lower()
    if nb.token_version not in ("v1", "v2"):
        raise HTTPException(
            status_code=400,
            detail="Invalid token_version. Must be 'v1' or 'v2'.",
        )
    if nb.token_version == "v1":
        nb.token_key = None
    elif nb.token_key is not None:
        stripped = nb.token_key.strip()
        nb.token_key = stripped if stripped else None
    nb.token = (nb.token or "").strip()


def _validate_netbox_credentials(nb: NetBoxEndpoint) -> None:
    secret = (nb.token or "").strip()
    key = (nb.token_key or "").strip() if nb.token_key else ""
    if nb.token_version == "v1":
        if not secret:
            raise HTTPException(
                status_code=400,
                detail="token is required for NetBox API token v1",
            )
        nb.token = secret
        return
    if not secret or not key:
        raise HTTPException(
            status_code=400,
            detail="token_key and token (secret) must both be set for NetBox API token v2",
        )
    nb.token = secret
    nb.token_key = key


def _encrypt_credentials(nb: NetBoxEndpoint) -> None:
    nb.token = nb.set_encrypted_token(nb.token) if hasattr(nb, "set_encrypted_token") else nb.token
    if nb.token_key and hasattr(nb, "set_encrypted_token_key"):
        nb.set_encrypted_token_key(nb.token_key)


@router.post("/endpoint", response_model=NetBoxEndpointResponse)
async def create_netbox_endpoint(
    netbox: NetBoxEndpointCreate, session: SessionDep
) -> NetBoxEndpointResponse:
    existing_any_result = await _maybe_await(session.exec(select(NetBoxEndpoint)))
    existing_any = existing_any_result.first()
    if existing_any:
        raise HTTPException(status_code=400, detail="Only one NetBox endpoint is allowed")

    existing_name_result = await _maybe_await(
        session.exec(select(NetBoxEndpoint).where(NetBoxEndpoint.name == netbox.name))
    )
    if existing_name_result.first():
        raise HTTPException(status_code=400, detail="NetBox endpoint name already exists")

    # Auto-allow the endpoint's own addresses so they pass SSRF validation below.
    pre_allow_endpoint_hosts(netbox.ip_address, netbox.domain or "", source="NetBox")

    settings = get_settings()
    ip_safe, ip_reason = validate_endpoint_host(netbox.ip_address, settings)
    if not ip_safe:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid IP address: {ip_reason}. Adjust SSRF settings in ProxboxPluginSettings.",
        )

    if netbox.domain:
        domain_safe, domain_reason = validate_endpoint_host(netbox.domain, settings)
        if not domain_safe:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid domain: {domain_reason}. Adjust SSRF settings in ProxboxPluginSettings.",
            )

    db_endpoint = NetBoxEndpoint(**netbox.model_dump())
    _normalize_netbox_endpoint_fields(db_endpoint)
    _validate_netbox_credentials(db_endpoint)

    db_endpoint.set_encrypted_token(db_endpoint.token)
    if db_endpoint.token_key:
        db_endpoint.set_encrypted_token_key(db_endpoint.token_key)

    session.add(db_endpoint)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(db_endpoint))
    clear_endpoint_cache()
    return NetBoxEndpointResponse.model_validate(db_endpoint)


@router.get("/endpoint", response_model=list[NetBoxEndpointResponse])
async def get_netbox_endpoints(
    session: SessionDep, offset: int = 0, limit: Annotated[int, Query(le=100)] = 100
) -> list[NetBoxEndpointResponse]:
    result = await _maybe_await(session.exec(select(NetBoxEndpoint).offset(offset).limit(limit)))
    netbox_endpoints = result.all()
    return [NetBoxEndpointResponse.model_validate(ep) for ep in netbox_endpoints]


GetNetBoxEndpoint = Annotated[list[NetBoxEndpointResponse], Depends(get_netbox_endpoints)]


@router.get("/endpoint/{netbox_id}", response_model=NetBoxEndpointResponse)
async def get_netbox_endpoint(netbox_id: int, session: SessionDep) -> NetBoxEndpointResponse:
    netbox_endpoint = await _maybe_await(session.get(NetBoxEndpoint, netbox_id))
    if not netbox_endpoint:
        raise HTTPException(status_code=404, detail="Netbox Endpoint not found")
    return NetBoxEndpointResponse.model_validate(netbox_endpoint)


@router.put("/endpoint/{netbox_id}", response_model=NetBoxEndpointResponse)
async def update_netbox_endpoint(
    netbox_id: int, netbox: NetBoxEndpointUpdate, session: SessionDep
) -> NetBoxEndpointResponse:
    db_netbox = await _maybe_await(session.get(NetBoxEndpoint, netbox_id))
    if not db_netbox:
        raise HTTPException(status_code=404, detail="NetBox Endpoint not found")

    update_data = netbox.model_dump(exclude_unset=True)

    # Auto-allow any new addresses so they pass SSRF validation below.
    pre_allow_endpoint_hosts(
        update_data.get("ip_address", ""),
        update_data.get("domain", ""),
        source="NetBox",
    )

    settings = get_settings()
    if "ip_address" in update_data:
        ip_safe, ip_reason = validate_endpoint_host(update_data["ip_address"], settings)
        if not ip_safe:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid IP address: {ip_reason}. Adjust SSRF settings in ProxboxPluginSettings.",
            )

    if "domain" in update_data and update_data["domain"]:
        domain_safe, domain_reason = validate_endpoint_host(update_data["domain"], settings)
        if not domain_safe:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid domain: {domain_reason}. Adjust SSRF settings in ProxboxPluginSettings.",
            )

    if "token" in update_data:
        update_data["token"] = (update_data["token"] or "").strip()

    for key, value in update_data.items():
        setattr(db_netbox, key, value)

    _normalize_netbox_endpoint_fields(db_netbox)
    _validate_netbox_credentials(db_netbox)

    db_netbox.set_encrypted_token(db_netbox.token)
    if db_netbox.token_key:
        db_netbox.set_encrypted_token_key(db_netbox.token_key)

    session.add(db_netbox)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(db_netbox))
    clear_endpoint_cache()
    return NetBoxEndpointResponse.model_validate(db_netbox)


@router.delete("/endpoint/{netbox_id}")
async def delete_netbox_endpoint(netbox_id: int, session: SessionDep) -> dict:
    netbox_endpoint = await _maybe_await(session.get(NetBoxEndpoint, netbox_id))
    if not netbox_endpoint:
        raise HTTPException(status_code=404, detail="Netbox Endpoint not found.")
    await _maybe_await(session.delete(netbox_endpoint))
    await _maybe_await(session.commit())
    clear_endpoint_cache()
    return {"message": "NetBox Endpoint deleted."}


@router.get("/status")
async def netbox_status(netbox_session: NetBoxSessionDep):
    """
    ### Asynchronously retrieves the status of the Netbox session.

    **Returns:**
    - The status of the Netbox session.
    """

    try:
        return await netbox_session.status()
    except Exception as error:
        raise ProxboxException(
            message="Error fetching status from NetBox API.",
            python_exception=str(error),
        )


@router.get("/openapi")
async def netbox_openapi(netbox_session: NetBoxSessionDep):
    """
    ### Fetches the OpenAPI documentation from the Netbox session.

    **Returns:**
    - **dict:** The OpenAPI documentation retrieved from the Netbox session.
    """

    try:
        output = await netbox_session.openapi()
        return output
    except Exception as error:
        raise ProxboxException(
            message="Error fetching OpenAPI documentation from NetBox API.",
            python_exception=str(error),
        )
