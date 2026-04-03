"""CRUD routes for local Proxmox endpoint records."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlmodel import select

from proxbox_api.database import DatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint

router = APIRouter()


class ProxmoxEndpointCreate(BaseModel):
    name: str
    ip_address: str
    domain: str | None = None
    port: int = 8006
    username: str
    password: str | None = None
    verify_ssl: bool = True
    token_name: str | None = None
    token_value: str | None = None


class ProxmoxEndpointUpdate(BaseModel):
    name: str | None = None
    ip_address: str | None = None
    domain: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    verify_ssl: bool | None = None
    token_name: str | None = None
    token_value: str | None = None


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
def create_proxmox_endpoint(
    endpoint: ProxmoxEndpointCreate,
    session: SessionDep,
) -> ProxmoxEndpointPublic:
    _validate_auth_fields(endpoint.password, endpoint.token_name, endpoint.token_value)

    existing = session.exec(
        select(ProxmoxEndpoint).where(ProxmoxEndpoint.name == endpoint.name)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Proxmox endpoint name already exists")

    db_endpoint = ProxmoxEndpoint(**endpoint.model_dump())
    session.add(db_endpoint)
    session.commit()
    session.refresh(db_endpoint)
    return _to_public_endpoint(db_endpoint)


@router.get("/endpoints")
def get_proxmox_endpoints(
    session: SessionDep,
    offset: int = 0,
    limit: Annotated[int, Query(le=100)] = 100,
) -> list[ProxmoxEndpointPublic]:
    endpoints = session.exec(select(ProxmoxEndpoint).offset(offset).limit(limit)).all()
    return [_to_public_endpoint(endpoint) for endpoint in endpoints]


@router.get("/endpoints/{endpoint_id}")
def get_proxmox_endpoint(endpoint_id: int, session: SessionDep) -> ProxmoxEndpointPublic:
    endpoint = session.get(ProxmoxEndpoint, endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Proxmox endpoint not found")
    return _to_public_endpoint(endpoint)


@router.put("/endpoints/{endpoint_id}")
def update_proxmox_endpoint(
    endpoint_id: int,
    endpoint: ProxmoxEndpointUpdate,
    session: SessionDep,
) -> ProxmoxEndpointPublic:
    db_endpoint = session.get(ProxmoxEndpoint, endpoint_id)
    if not db_endpoint:
        raise HTTPException(status_code=404, detail="Proxmox endpoint not found")

    update_data = endpoint.model_dump(exclude_unset=True)

    if "name" in update_data:
        existing = session.exec(
            select(ProxmoxEndpoint).where(ProxmoxEndpoint.name == update_data["name"])
        ).first()
        if existing and existing.id != endpoint_id:
            raise HTTPException(status_code=400, detail="Proxmox endpoint name already exists")

    new_password = update_data.get("password", db_endpoint.password)
    new_token_name = update_data.get("token_name", db_endpoint.token_name)
    new_token_value = update_data.get("token_value", db_endpoint.token_value)
    _validate_auth_fields(new_password, new_token_name, new_token_value)

    for key, value in update_data.items():
        setattr(db_endpoint, key, value)

    session.add(db_endpoint)
    session.commit()
    session.refresh(db_endpoint)
    return _to_public_endpoint(db_endpoint)


@router.delete("/endpoints/{endpoint_id}")
def delete_proxmox_endpoint(endpoint_id: int, session: SessionDep) -> dict[str, str]:
    endpoint = session.get(ProxmoxEndpoint, endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Proxmox endpoint not found")

    session.delete(endpoint)
    session.commit()
    return {"message": "Proxmox endpoint deleted."}
