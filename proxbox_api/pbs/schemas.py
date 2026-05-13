"""Public response shapes for the ``/pbs/*`` routes.

Internal PBS payloads come from :mod:`proxmox_sdk.pbs.models`; these are the
serialization wrappers that surface in the FastAPI OpenAPI schema and that the
NetBox-side mappers consume.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PBSStatusItem(BaseModel):
    """One PBSEndpoint's reachability snapshot."""

    model_config = ConfigDict(from_attributes=True)

    endpoint_id: int
    name: str
    host: str
    port: int
    reachable: bool
    reason: str | None = None
    version: str | None = None


class PBSStatusResponse(BaseModel):
    items: list[PBSStatusItem]


class PBSSyncSummary(BaseModel):
    """Per-endpoint per-resource sync stats returned by ``/pbs/sync/*``."""

    endpoint_id: int
    name: str
    resource: Literal["datastores", "snapshots", "jobs", "node", "full"]
    fetched: int = Field(default=0, ge=0)
    written: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)
    netbox_branch_schema_id: str | None = None


class PBSSyncResponse(BaseModel):
    items: list[PBSSyncSummary]
    raw: dict[str, Any] | None = None


__all__ = [
    "PBSStatusItem",
    "PBSStatusResponse",
    "PBSSyncSummary",
    "PBSSyncResponse",
]
