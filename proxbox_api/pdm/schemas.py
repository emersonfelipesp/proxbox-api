"""Public response shapes for the ``/pdm/*`` routes.

Internal PDM payloads come from :mod:`proxmox_sdk.pdm.models`; these are the
serialization wrappers that surface in the FastAPI OpenAPI schema and that the
NetBox-side mappers consume.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PDMStatusItem(BaseModel):
    """One PDMEndpoint's reachability snapshot."""

    model_config = ConfigDict(from_attributes=True)

    endpoint_id: int
    name: str
    host: str
    port: int
    reachable: bool
    reason: str | None = None
    version: str | None = None


class PDMStatusResponse(BaseModel):
    items: list[PDMStatusItem]


class PDMSyncSummary(BaseModel):
    """Per-endpoint per-resource sync stats returned by ``/pdm/sync/*``."""

    endpoint_id: int
    name: str
    resource: Literal["remotes", "guests", "datastores", "resources", "full"]
    fetched: int = Field(default=0, ge=0)
    written: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)
    netbox_branch_schema_id: str | None = None


class PDMSyncResponse(BaseModel):
    items: list[PDMSyncSummary]


__all__ = [
    "PDMStatusItem",
    "PDMStatusResponse",
    "PDMSyncSummary",
    "PDMSyncResponse",
]
