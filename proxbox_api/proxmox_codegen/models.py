"""Typed models used by the Proxmox API Viewer code generation pipeline."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

HTTP_METHODS = ("GET", "POST", "PUT", "DELETE")


class RawMethodCapture(BaseModel):
    """Captured method payload from the API Viewer UI and apidoc tree."""

    method: str
    path: str
    method_name: str | None = None
    description: str | None = None
    parameters: dict[str, Any] | None = None
    returns: dict[str, Any] | None = None
    permissions: dict[str, Any] | None = None
    allowtoken: int | str | None = None
    protected: int | str | None = None
    unstable: int | str | None = None
    raw_sections: list[str] = Field(default_factory=list)
    source: str = "viewer"


class EndpointCapture(BaseModel):
    """Captured endpoint node from API Viewer traversal."""

    path: str
    text: str | None = None
    leaf: int | bool | None = None
    methods: dict[str, RawMethodCapture] = Field(default_factory=dict)


class NormalizedOperation(BaseModel):
    """Normalized operation model ready for OpenAPI conversion."""

    method: str
    path: str
    operation_id: str
    summary: str | None = None
    description: str | None = None
    path_params: list[dict[str, Any]] = Field(default_factory=list)
    query_params: list[dict[str, Any]] = Field(default_factory=list)
    request_body_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class GenerationBundle(BaseModel):
    """Final bundle returned by the Proxmox generation pipeline."""

    source_url: str
    version_tag: str
    generated_at: str
    endpoint_count: int
    operation_count: int
    capture: dict[str, Any]
    openapi: dict[str, Any]
    pydantic_models_code: str
