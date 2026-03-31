"""
Tests for stub routes that return 501 Not Implemented.

These routes are placeholders for future functionality and should
return consistent 501 responses until implemented.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from proxbox_api.main import app


class TestVirtualizationStubRoutes:
    """Test stub routes under /virtualization that return 501."""

    @pytest.mark.asyncio
    async def test_cluster_types_create_returns_501(self):
        """GET /virtualization/cluster-types/create should return 501."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/cluster-types/create")
            assert resp.status_code == 501
            data = resp.json()
            assert "detail" in data
            assert "not implemented" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_clusters_create_returns_501(self):
        """GET /virtualization/clusters/create should return 501."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/clusters/create")
            assert resp.status_code == 501
            data = resp.json()
            assert "detail" in data
            assert "not implemented" in data["detail"].lower()


class TestVirtualMachineStubRoutes:
    """Test stub routes for VM read operations that return 501."""

    @pytest.mark.asyncio
    async def test_vm_summary_by_id_returns_501(self):
        """GET /virtualization/virtual-machines/{id}/summary should return 501."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/123/summary")
            assert resp.status_code == 501
            data = resp.json()
            assert "detail" in data
            assert "not implemented" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_vm_interfaces_create_returns_501(self):
        """GET /virtualization/virtual-machines/interfaces/create should return 501."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/interfaces/create")
            assert resp.status_code == 501
            data = resp.json()
            assert "detail" in data
            assert "not implemented" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_vm_interfaces_ip_address_create_returns_501(self):
        """GET /virtualization/virtual-machines/interfaces/ip-address/create should return 501."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/interfaces/ip-address/create")
            assert resp.status_code == 501
            data = resp.json()
            assert "detail" in data
            assert "not implemented" in data["detail"].lower()


class TestVirtualMachineReadRoutes:
    """Test routes that are implemented (not501)."""

    @pytest.mark.asyncio
    async def test_vm_list_is_implemented(self):
        """GET /virtualization/virtual-machines/ should be implemented."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_vm_get_by_id_is_implemented(self):
        """GET /virtualization/virtual-machines/{id} should be implemented."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/1")
            assert resp.status_code in (200, 404), f"Expected 200 or 404, got {resp.status_code}"

    @pytest.mark.asyncio
    async def test_vm_summary_example_is_implemented(self):
        """GET /virtualization/virtual-machines/summary/example should return example data."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/virtualization/virtual-machines/summary/example")
            assert resp.status_code == 200
            data = resp.json()
            assert "id" in data
            assert "name" in data
            assert "status" in data
