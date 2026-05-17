"""CRUD routes for local PDM endpoint records (``/pdm/endpoints``)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import PDMEndpoint
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()


class PDMEndpointCreate(BaseModel):
    name: str = Field(max_length=255)
    host: str = Field(max_length=255)
    port: int = Field(default=8443, ge=1, le=65535)
    token_id: str = Field(max_length=255)
    token_secret: str = Field(max_length=2000)
    fingerprint: str | None = Field(default=None, max_length=200)
    verify_ssl: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=600)


class PDMEndpointUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    token_id: str | None = Field(default=None, max_length=255)
    token_secret: str | None = Field(default=None, max_length=2000)
    fingerprint: str | None = Field(default=None, max_length=200)
    verify_ssl: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)


class PDMEndpointPublic(BaseModel):
    """Public PDM endpoint shape with credentials redacted."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    name: str
    host: str
    port: int
    token_id: str
    fingerprint: str | None = None
    verify_ssl: bool
    allow_writes: bool
    timeout_seconds: int


def _to_public(endpoint: PDMEndpoint) -> PDMEndpointPublic:
    return PDMEndpointPublic.model_validate(endpoint)


@router.post("/endpoints", response_model=PDMEndpointPublic)
async def create_pdm_endpoint(
    endpoint: PDMEndpointCreate,
    session: SessionDep,
) -> PDMEndpointPublic:
    existing_result = await _maybe_await(
        session.exec(select(PDMEndpoint).where(PDMEndpoint.name == endpoint.name))
    )
    if existing_result.first():
        raise HTTPException(status_code=400, detail="PDM endpoint name already exists")

    db_endpoint = PDMEndpoint(**endpoint.model_dump())
    db_endpoint.set_encrypted_token_secret(endpoint.token_secret)

    session.add(db_endpoint)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(db_endpoint))
    return _to_public(db_endpoint)


@router.get("/endpoints", response_model=list[PDMEndpointPublic])
async def list_pdm_endpoints(
    session: SessionDep,
    offset: int = 0,
    limit: Annotated[int, Query(le=100)] = 100,
) -> list[PDMEndpointPublic]:
    result = await _maybe_await(session.exec(select(PDMEndpoint).offset(offset).limit(limit)))
    return [_to_public(item) for item in result.all()]


@router.get("/endpoints/{endpoint_id}", response_model=PDMEndpointPublic)
async def get_pdm_endpoint(endpoint_id: int, session: SessionDep) -> PDMEndpointPublic:
    endpoint = await _maybe_await(session.get(PDMEndpoint, endpoint_id))
    if not endpoint:
        raise HTTPException(status_code=404, detail="PDM endpoint not found")
    return _to_public(endpoint)


@router.put("/endpoints/{endpoint_id}", response_model=PDMEndpointPublic)
async def update_pdm_endpoint(
    endpoint_id: int,
    endpoint: PDMEndpointUpdate,
    session: SessionDep,
) -> PDMEndpointPublic:
    db_endpoint = await _maybe_await(session.get(PDMEndpoint, endpoint_id))
    if not db_endpoint:
        raise HTTPException(status_code=404, detail="PDM endpoint not found")

    update_data = endpoint.model_dump(exclude_unset=True)

    if "name" in update_data:
        existing_result = await _maybe_await(
            session.exec(select(PDMEndpoint).where(PDMEndpoint.name == update_data["name"]))
        )
        existing = existing_result.first()
        if existing and existing.id != endpoint_id:
            raise HTTPException(status_code=400, detail="PDM endpoint name already exists")

    new_secret = update_data.pop("token_secret", None)
    for key, value in update_data.items():
        setattr(db_endpoint, key, value)
    if new_secret is not None:
        db_endpoint.set_encrypted_token_secret(new_secret)

    session.add(db_endpoint)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(db_endpoint))
    return _to_public(db_endpoint)


@router.delete("/endpoints/{endpoint_id}")
async def delete_pdm_endpoint(endpoint_id: int, session: SessionDep) -> dict[str, str]:
    endpoint = await _maybe_await(session.get(PDMEndpoint, endpoint_id))
    if not endpoint:
        raise HTTPException(status_code=404, detail="PDM endpoint not found")
    await _maybe_await(session.delete(endpoint))
    await _maybe_await(session.commit())
    return {"message": "PDM endpoint deleted."}


__all__ = ["router"]
