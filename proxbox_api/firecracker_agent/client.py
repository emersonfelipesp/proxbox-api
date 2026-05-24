"""Async HTTP client for the Firecracker host-agent API."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from proxbox_api.schemas.firecracker import (
    FirecrackerAssetPrepareRequest,
    FirecrackerAssetPrepareResponse,
    FirecrackerHostAgentHealth,
    FirecrackerHostCapabilities,
    FirecrackerMicroVMAction,
    FirecrackerMicroVMCreateRequest,
    FirecrackerMicroVMMetrics,
    FirecrackerMicroVMState,
)


class FirecrackerHostAgentError(RuntimeError):
    """Raised when a host-agent request fails."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FirecrackerHostAgentClient:
    """Small token-aware client for a single Firecracker host-agent."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    url,
                    json=json,
                    headers=self._headers(),
                )
        except httpx.HTTPError as error:
            raise FirecrackerHostAgentError(str(error)) from error

        if response.status_code >= 400:
            detail: str
            try:
                payload = response.json()
            except ValueError:
                detail = response.text
            else:
                detail = (
                    str(payload.get("detail", payload))
                    if isinstance(payload, dict)
                    else str(payload)
                )
            raise FirecrackerHostAgentError(detail, status_code=response.status_code)

        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise FirecrackerHostAgentError("host-agent response was not JSON object/list")
        return payload

    async def health(self) -> FirecrackerHostAgentHealth:
        payload = await self._request("GET", "/health")
        return FirecrackerHostAgentHealth.model_validate(payload)

    async def capabilities(self) -> FirecrackerHostCapabilities:
        payload = await self._request("GET", "/capabilities")
        return FirecrackerHostCapabilities.model_validate(payload)

    async def prepare_assets(
        self,
        request: FirecrackerAssetPrepareRequest,
    ) -> FirecrackerAssetPrepareResponse:
        payload = await self._request(
            "POST",
            "/assets/prepare",
            json=request.model_dump(mode="json"),
        )
        return FirecrackerAssetPrepareResponse.model_validate(payload)

    async def create_microvm(
        self,
        request: FirecrackerMicroVMCreateRequest,
    ) -> FirecrackerMicroVMState:
        payload = await self._request(
            "POST",
            "/microvms",
            json=request.model_dump(mode="json"),
        )
        return FirecrackerMicroVMState.model_validate(payload)

    async def action(
        self,
        microvm_id: UUID,
        action: FirecrackerMicroVMAction,
    ) -> FirecrackerMicroVMState:
        payload = await self._request(
            "POST",
            f"/microvms/{microvm_id}/actions/{action.value}",
        )
        return FirecrackerMicroVMState.model_validate(payload)

    async def metrics(self, microvm_id: UUID) -> FirecrackerMicroVMMetrics:
        payload = await self._request("GET", f"/microvms/{microvm_id}/metrics")
        return FirecrackerMicroVMMetrics.model_validate(payload)

    async def logs(self, microvm_id: UUID, *, tail: int = 200) -> list[str]:
        payload = await self._request("GET", f"/microvms/{microvm_id}/logs?tail={tail}")
        if isinstance(payload, dict):
            lines = payload.get("lines", [])
        else:
            lines = payload
        return [str(line) for line in lines]
