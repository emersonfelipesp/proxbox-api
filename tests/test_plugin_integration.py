"""
Plugin Integration Tests

Tests that verify the proxbox-api backend correctly handles requests from the
netbox-proxbox plugin, including endpoint schema compatibility, query parameter
handling, and response format validation.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from sqlmodel import Session
from proxbox_api.main import app
from proxbox_api.database import NetBoxEndpoint, ProxmoxEndpoint


class TestPluginAPIPath:
    """Test paths expected by the plugin."""

    @pytest.mark.asyncio
    async def test_devices_create_path_exists(self):
        """Plugin expects /dcim/devices/create endpoint."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/dcim/devices/create")
            # Should not return 404 (might fail for other reasons like missing endpoints)
            assert resp.status_code != 404

    @pytest.mark.asyncio
    async def test_vms_create_path_exists(self):
        """Plugin expects /virtualization/virtual-machines/create endpoint."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/create")
            assert resp.status_code != 404

    @pytest.mark.asyncio
    async def test_backups_create_path_exists(self):
        """Plugin expects /virtualization/virtual-machines/backups/all/create endpoint."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/backups/all/create")
            assert resp.status_code != 404

    @pytest.mark.asyncio
    async def test_snapshots_create_path_exists(self):
        """Plugin expects /virtualization/virtual-machines/snapshots/all/create endpoint."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/snapshots/all/create")
            assert resp.status_code != 404

    @pytest.mark.asyncio
    async def test_storage_create_path_exists(self):
        """Plugin expects /virtualization/virtual-machines/storage/create endpoint."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/storage/create")
            assert resp.status_code != 404

    @pytest.mark.asyncio
    async def test_virtual_disks_create_path_exists(self):
        """Plugin expects /virtualization/virtual-machines/virtual-disks/create endpoint."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/virtual-disks/create")
            assert resp.status_code != 404

    @pytest.mark.asyncio
    async def test_full_update_path_exists(self):
        """Plugin expects /full-update endpoint."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/full-update")
            assert resp.status_code != 404


class TestStreamEndpoints:
    """Test stream endpoint variants."""

    @pytest.mark.asyncio
    async def test_devices_create_stream_path_exists(self):
        """Plugin expects /dcim/devices/create/stream endpoint."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream("GET", "/dcim/devices/create/stream") as resp:
                # Path exists if status is not 404 or 405
                assert resp.status_code != 404
                assert resp.status_code != 405

    @pytest.mark.asyncio
    async def test_vms_create_stream_path_exists(self):
        """Plugin expects /virtualization/virtual-machines/create/stream endpoint."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream(
                "GET", "/virtualization/virtual-machines/create/stream"
            ) as resp:
                assert resp.status_code != 404
                assert resp.status_code != 405

    @pytest.mark.asyncio
    async def test_full_update_stream_path_exists(self):
        """Plugin expects /full-update/stream endpoint."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream("GET", "/full-update/stream") as resp:
                assert resp.status_code != 404
                assert resp.status_code != 405


class TestNonStreamEndpoints:
    """Test non-stream endpoints return JSON."""

    @pytest.mark.asyncio
    async def test_root_returns_json(self):
        """Root endpoint should return JSON."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/")
            assert resp.status_code == 200
            assert resp.headers.get("content-type", "").startswith("application/json")
            data = resp.json()
            assert isinstance(data, dict)
