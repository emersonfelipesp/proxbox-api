"""CRUD routes for local Proxmox endpoint records."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlmodel import select

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.enum.proxmox import ProxmoxAccessMethod
from proxbox_api.schemas.cloud_image_security import (
    CloudImageSSHExecutionTarget,
    normalize_ssh_fingerprint,
    normalize_ssh_host,
    normalize_ssh_identity_file,
    normalize_ssh_user,
)
from proxbox_api.settings_client import get_settings
from proxbox_api.ssrf import clear_endpoint_cache, pre_allow_endpoint_hosts, validate_endpoint_host
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()

_ACCESS_METHOD_VALUES = tuple(m.value for m in ProxmoxAccessMethod)
_SSH_BINDING_FIELDS = (
    "ssh_target_node",
    "ssh_host",
    "ssh_username",
    "ssh_identity_file",
    "ssh_known_host_fingerprint",
)


def _validate_complete_ssh_binding(values: dict[str, object]) -> None:
    """Require the Cloud Image Pipeline SSH binding to be complete or absent."""

    populated = [field for field in _SSH_BINDING_FIELDS if values.get(field)]
    if not populated:
        return
    missing = [field for field in _SSH_BINDING_FIELDS if not values.get(field)]
    if missing:
        raise ValueError(
            "Cloud Image Pipeline SSH binding is incomplete; missing " + ", ".join(missing)
        )
    port = values.get("ssh_port", 22)
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError("Cloud Image Pipeline SSH port must be an integer.")
    CloudImageSSHExecutionTarget(
        host=str(values["ssh_host"]),
        user=str(values["ssh_username"]),
        port=port,
        identity_file=str(values["ssh_identity_file"]),
        known_host_fingerprint=str(values["ssh_known_host_fingerprint"]),
    )


def _normalize_ssh_target_node(value: str | None) -> str | None:
    if value is None:
        return None
    node = value.strip()
    if not node:
        raise ValueError("ssh_target_node must not be blank.")
    return node


def _validate_access_methods(value: str | None) -> str | None:
    """Accept only ``api`` / ``api_ssh``; reject SSH-only and unknown values.

    Returning a 422 (via ``ValueError``) makes ``ssh`` and any other value
    impossible to persist, enforcing the "API is mandatory, SSH-only is not
    allowed" invariant at the API boundary.
    """
    if value is None:
        return None
    if value not in _ACCESS_METHOD_VALUES:
        raise ValueError(
            f"access_methods must be one of {list(_ACCESS_METHOD_VALUES)} "
            "(SSH-only is not allowed; SSH only complements API)"
        )
    return value


class ProxmoxEndpointCreate(BaseModel):
    name: str = Field(max_length=255)
    ip_address: str = Field(max_length=45)
    domain: str | None = Field(default=None, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(max_length=255)
    password: str | None = Field(default=None, max_length=1000)
    verify_ssl: bool = True
    enabled: bool = True
    allow_writes: bool = False
    access_methods: str = ProxmoxAccessMethod.api.value
    ssh_target_node: str | None = Field(default=None, min_length=1, max_length=255)
    ssh_host: str | None = Field(default=None, max_length=255)
    ssh_username: str | None = Field(default=None, max_length=64)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_identity_file: str | None = Field(default=None, max_length=4096)
    ssh_known_host_fingerprint: str | None = Field(default=None, max_length=128)
    token_name: str | None = Field(default=None, max_length=255)
    token_value: str | None = Field(default=None, max_length=1000)
    site_id: int | None = Field(default=None, ge=1)
    site_slug: str | None = Field(default=None, max_length=255)
    site_name: str | None = Field(default=None, max_length=255)
    tenant_id: int | None = Field(default=None, ge=1)
    tenant_slug: str | None = Field(default=None, max_length=255)
    tenant_name: str | None = Field(default=None, max_length=255)
    timeout: int | None = Field(default=None, ge=1, le=3600)
    max_retries: int | None = Field(default=None, ge=0, le=100)
    retry_backoff: float | None = Field(default=None, ge=0.0, le=300.0)

    @field_validator("access_methods")
    @classmethod
    def _check_access_methods(cls, value: str) -> str:
        validated = _validate_access_methods(value)
        return validated if validated is not None else ProxmoxAccessMethod.api.value

    @field_validator("ssh_host")
    @classmethod
    def _check_ssh_host(cls, value: str | None) -> str | None:
        return normalize_ssh_host(value) if value is not None else None

    @field_validator("ssh_target_node")
    @classmethod
    def _check_ssh_target_node(cls, value: str | None) -> str | None:
        return _normalize_ssh_target_node(value)

    @field_validator("ssh_username")
    @classmethod
    def _check_ssh_username(cls, value: str | None) -> str | None:
        return normalize_ssh_user(value) if value is not None else None

    @field_validator("ssh_identity_file")
    @classmethod
    def _check_ssh_identity_file(cls, value: str | None) -> str | None:
        return normalize_ssh_identity_file(value) if value is not None else None

    @field_validator("ssh_known_host_fingerprint")
    @classmethod
    def _check_ssh_fingerprint(cls, value: str | None) -> str | None:
        return normalize_ssh_fingerprint(value) if value is not None else None

    @model_validator(mode="after")
    def _check_complete_ssh_binding(self) -> "ProxmoxEndpointCreate":
        _validate_complete_ssh_binding(self.model_dump())
        return self


class ProxmoxEndpointUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=45)
    domain: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=1000)
    verify_ssl: bool | None = None
    enabled: bool | None = None
    allow_writes: bool | None = None
    access_methods: str | None = None
    ssh_target_node: str | None = Field(default=None, min_length=1, max_length=255)
    ssh_host: str | None = Field(default=None, max_length=255)
    ssh_username: str | None = Field(default=None, max_length=64)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_identity_file: str | None = Field(default=None, max_length=4096)
    ssh_known_host_fingerprint: str | None = Field(default=None, max_length=128)
    token_name: str | None = Field(default=None, max_length=255)
    token_value: str | None = Field(default=None, max_length=1000)
    site_id: int | None = Field(default=None, ge=1)
    site_slug: str | None = Field(default=None, max_length=255)
    site_name: str | None = Field(default=None, max_length=255)
    tenant_id: int | None = Field(default=None, ge=1)
    tenant_slug: str | None = Field(default=None, max_length=255)
    tenant_name: str | None = Field(default=None, max_length=255)
    timeout: int | None = Field(default=None, ge=1, le=3600)
    max_retries: int | None = Field(default=None, ge=0, le=100)
    retry_backoff: float | None = Field(default=None, ge=0.0, le=300.0)

    @field_validator("access_methods")
    @classmethod
    def _check_access_methods(cls, value: str | None) -> str | None:
        return _validate_access_methods(value)

    @field_validator("ssh_host")
    @classmethod
    def _check_ssh_host(cls, value: str | None) -> str | None:
        return normalize_ssh_host(value) if value is not None else None

    @field_validator("ssh_target_node")
    @classmethod
    def _check_ssh_target_node(cls, value: str | None) -> str | None:
        return _normalize_ssh_target_node(value)

    @field_validator("ssh_username")
    @classmethod
    def _check_ssh_username(cls, value: str | None) -> str | None:
        return normalize_ssh_user(value) if value is not None else None

    @field_validator("ssh_identity_file")
    @classmethod
    def _check_ssh_identity_file(cls, value: str | None) -> str | None:
        return normalize_ssh_identity_file(value) if value is not None else None

    @field_validator("ssh_known_host_fingerprint")
    @classmethod
    def _check_ssh_fingerprint(cls, value: str | None) -> str | None:
        return normalize_ssh_fingerprint(value) if value is not None else None

    @field_validator("ssh_port")
    @classmethod
    def _reject_null_ssh_port(cls, value: int | None) -> int | None:
        if value is None:
            raise ValueError("ssh_port cannot be null; omit it or provide an integer port.")
        return value


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
    enabled: bool
    allow_writes: bool
    access_methods: str = ProxmoxAccessMethod.api.value
    ssh_target_node: str | None = None
    ssh_host: str | None = None
    ssh_username: str | None = None
    ssh_port: int = 22
    ssh_identity_file: str | None = None
    ssh_known_host_fingerprint: str | None = None
    site_id: int | None = None
    site_slug: str | None = None
    site_name: str | None = None
    tenant_id: int | None = None
    tenant_slug: str | None = None
    tenant_name: str | None = None
    timeout: int | None = None
    max_retries: int | None = None
    retry_backoff: float | None = None


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

    merged_ssh_binding = {
        field: update_data.get(field, getattr(db_endpoint, field))
        for field in (*_SSH_BINDING_FIELDS, "ssh_port")
    }
    try:
        _validate_complete_ssh_binding(merged_ssh_binding)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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
