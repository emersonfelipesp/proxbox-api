"""CRUD routes for local Proxmox endpoint records."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.settings_client import get_settings
from proxbox_api.ssrf import clear_endpoint_cache, pre_allow_endpoint_hosts, validate_endpoint_host
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()


class ProxmoxEndpointCreate(BaseModel):
    name: str = Field(max_length=255)
    ip_address: str = Field(max_length=45)
    domain: str | None = Field(default=None, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(max_length=255)
    password: str | None = Field(default=None, max_length=1000)
    verify_ssl: bool = True
    token_name: str | None = Field(default=None, max_length=255)
    token_value: str | None = Field(default=None, max_length=1000)
    site_id: int | None = Field(default=None, ge=1)
    site_slug: str | None = Field(default=None, max_length=255)
    site_name: str | None = Field(default=None, max_length=255)
    tenant_id: int | None = Field(default=None, ge=1)
    tenant_slug: str | None = Field(default=None, max_length=255)
    tenant_name: str | None = Field(default=None, max_length=255)


class ProxmoxEndpointUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=45)
    domain: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=1000)
    verify_ssl: bool | None = None
    token_name: str | None = Field(default=None, max_length=255)
    token_value: str | None = Field(default=None, max_length=1000)
    site_id: int | None = Field(default=None, ge=1)
    site_slug: str | None = Field(default=None, max_length=255)
    site_name: str | None = Field(default=None, max_length=255)
    tenant_id: int | None = Field(default=None, ge=1)
    tenant_slug: str | None = Field(default=None, max_length=255)
    tenant_name: str | None = Field(default=None, max_length=255)


class ProxmoxEndpointPublic(BaseModel):
    """Public Proxmox endpoint shape with credentials redacted."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    name: str
    ip_address: str
    domain: str | None = None
    port: int
    username: str
    verify_ssl: bool
    site_id: int | None = None
    site_slug: str | None = None
    site_name: str | None = None
    tenant_id: int | None = None
    tenant_slug: str | None = None
    tenant_name: str | None = None


def _validate_auth_fields(
    password: str | None,
    token_name: str | None,
    token_value: str | None,
) -> None:
    has_password = bool(password)
    has_token_name = bool(token_name)
    has_token_value = bool(token_value)

    if has_token_name ^ has_token_value:
        raise HTTPException(
            status_code=400,
            detail="token_name and token_value must be provided together",
        )

    if not has_password and not (has_token_name and has_token_value):
        raise HTTPException(
            status_code=400,
            detail="Provide password or both token_name/token_value",
        )


def _to_public_endpoint(endpoint: ProxmoxEndpoint) -> ProxmoxEndpointPublic:
    return ProxmoxEndpointPublic.model_validate(endpoint)


@router.post("/endpoints")
async def create_proxmox_endpoint(
    endpoint: ProxmoxEndpointCreate,
    session: SessionDep,
) -> ProxmoxEndpointPublic:
    _validate_auth_fields(endpoint.password, endpoint.token_name, endpoint.token_value)

    # Auto-allow the endpoint's own addresses so they pass SSRF validation below.
    pre_allow_endpoint_hosts(endpoint.ip_address, endpoint.domain or "", source="Proxmox")

    settings = get_settings()
    ip_safe, ip_reason = validate_endpoint_host(endpoint.ip_address, settings)
    if not ip_safe:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid IP address: {ip_reason}",
        )

    if endpoint.domain:
        domain_safe, domain_reason = validate_endpoint_host(endpoint.domain, settings)
        if not domain_safe:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid domain: {domain_reason}",
            )

    existing_result = await _maybe_await(
        session.exec(select(ProxmoxEndpoint).where(ProxmoxEndpoint.name == endpoint.name))
    )
    existing = existing_result.first()
    if existing:
        raise HTTPException(status_code=400, detail="Proxmox endpoint name already exists")

    db_endpoint = ProxmoxEndpoint(**endpoint.model_dump())

    if db_endpoint.password:
        db_endpoint.set_encrypted_password(db_endpoint.password)
    if db_endpoint.token_value:
        db_endpoint.set_encrypted_token_value(db_endpoint.token_value)

    session.add(db_endpoint)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(db_endpoint))

    clear_endpoint_cache()
    return _to_public_endpoint(db_endpoint)


@router.get("/endpoints")
async def get_proxmox_endpoints(
    session: SessionDep,
    offset: int = 0,
    limit: Annotated[int, Query(le=100)] = 100,
) -> list[ProxmoxEndpointPublic]:
    result = await _maybe_await(session.exec(select(ProxmoxEndpoint).offset(offset).limit(limit)))
    endpoints = result.all()
    return [_to_public_endpoint(endpoint) for endpoint in endpoints]


@router.get("/endpoints/{endpoint_id}")
async def get_proxmox_endpoint(endpoint_id: int, session: SessionDep) -> ProxmoxEndpointPublic:
    endpoint = await _maybe_await(session.get(ProxmoxEndpoint, endpoint_id))
    if not endpoint:
        raise HTTPException(status_code=404, detail="Proxmox endpoint not found")
    return _to_public_endpoint(endpoint)


@router.put("/endpoints/{endpoint_id}")
async def update_proxmox_endpoint(  # noqa: C901
    endpoint_id: int,
    endpoint: ProxmoxEndpointUpdate,
    session: SessionDep,
) -> ProxmoxEndpointPublic:
    db_endpoint = await _maybe_await(session.get(ProxmoxEndpoint, endpoint_id))
    if not db_endpoint:
        raise HTTPException(status_code=404, detail="Proxmox Endpoint not found")

    update_data = endpoint.model_dump(exclude_unset=True)

    # Auto-allow any new addresses so they pass SSRF validation below.
    pre_allow_endpoint_hosts(
        update_data.get("ip_address", ""),
        update_data.get("domain", "") or "",
        source="Proxmox",
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

    if "name" in update_data:
        existing_result = await _maybe_await(
            session.exec(select(ProxmoxEndpoint).where(ProxmoxEndpoint.name == update_data["name"]))
        )
        existing = existing_result.first()
        if existing and existing.id != endpoint_id:
            raise HTTPException(status_code=400, detail="Proxmox endpoint name already exists")

    updating_password = "password" in update_data
    updating_token_name = "token_name" in update_data
    updating_token_value = "token_value" in update_data

    if updating_password or updating_token_name or updating_token_value:
        new_password = update_data.get("password")
        new_token_name = update_data.get("token_name")
        new_token_value = update_data.get("token_value")

        has_password = bool(new_password)
        has_token_name = bool(new_token_name)
        has_token_value = bool(new_token_value)

        if has_token_name ^ has_token_value:
            raise HTTPException(
                status_code=400,
                detail="token_name and token_value must be provided together",
            )

        if not has_password and not (has_token_name and has_token_value):
            raise HTTPException(
                status_code=400,
                detail="Provide password or both token_name/token_value",
            )

        if updating_password:
            db_endpoint.set_encrypted_password(update_data["password"])
        if updating_token_value:
            db_endpoint.set_encrypted_token_value(update_data["token_value"])

    for key, value in update_data.items():
        if key not in ("password", "token_value"):
            setattr(db_endpoint, key, value)

    session.add(db_endpoint)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(db_endpoint))

    clear_endpoint_cache()
    return _to_public_endpoint(db_endpoint)


@router.delete("/endpoints/{endpoint_id}")
async def delete_proxmox_endpoint(endpoint_id: int, session: SessionDep) -> dict[str, str]:
    endpoint = await _maybe_await(session.get(ProxmoxEndpoint, endpoint_id))
    if not endpoint:
        raise HTTPException(status_code=404, detail="Proxmox endpoint not found")

    await _maybe_await(session.delete(endpoint))
    await _maybe_await(session.commit())

    clear_endpoint_cache()
    return {"message": "Proxmox endpoint deleted."}
