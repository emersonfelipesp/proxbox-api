"""E2E smoke tests for service-specific proxmox-sdk mock images."""

from __future__ import annotations

import os

import httpx
import pytest


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.mock_http
async def test_pbs_pdm_mock_root_reports_loaded_service(proxmox_service: str):
    """Verify PBS/PDM root endpoint reports the loaded service stub."""
    if proxmox_service == "pve":
        pytest.skip("PBS/PDM service smoke only")

    base_url = os.environ.get("PROXMOX_MOCK_PUBLISHED_URL", "http://localhost:8006").rstrip("/")
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        health = await client.get("/health")
        root = await client.get("/")

    assert health.status_code == 200, f"/health returned {health.status_code}: {health.text}"
    assert root.status_code == 200, f"/ returned {root.status_code}: {root.text}"
    assert proxmox_service in root.text.lower(), (
        f"root payload did not identify service={proxmox_service}: {root.text}"
    )
