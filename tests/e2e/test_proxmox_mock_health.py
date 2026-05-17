"""E2E smoke tests for service-specific proxmox-sdk mock images."""

from __future__ import annotations

import os

import httpx
import pytest


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.mock_http
async def test_pbs_pdm_mock_health_reports_loaded_service(proxmox_service: str):
    """Verify PBS/PDM health reports the loaded service stub."""
    if proxmox_service == "pve":
        pytest.skip("PBS/PDM service smoke only")

    base_url = os.environ.get("PROXMOX_MOCK_PUBLISHED_URL", "http://localhost:8006").rstrip("/")
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert proxmox_service in response.text.lower(), (
        f"health payload did not identify service={proxmox_service}: {response.text}"
    )
