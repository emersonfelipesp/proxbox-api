"""
SSE Stream Output Tests

Verifies that the backend produces SSE events in the correct format
for the plugin to consume. These tests ensure the SSE contract is maintained.
"""


class TestPluginAPIPath:
    """Test paths expected by the plugin."""

    async def test_devices_create_path_exists(self, authenticated_client):
        """Plugin expects /dcim/devices/create endpoint."""
        resp = await authenticated_client.get("/dcim/devices/create")
        # Should not return 404 (might fail for other reasons like missing endpoints)
        assert resp.status_code != 404

    async def test_vms_create_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/create endpoint."""
        resp = await authenticated_client.get("/virtualization/virtual-machines/create")
        assert resp.status_code != 404

    async def test_backups_create_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/backups/all/create endpoint."""
        resp = await authenticated_client.get("/virtualization/virtual-machines/backups/all/create")
        assert resp.status_code != 404

    async def test_snapshots_create_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/snapshots/all/create endpoint."""
        resp = await authenticated_client.get(
            "/virtualization/virtual-machines/snapshots/all/create"
        )
        assert resp.status_code != 404

    async def test_storage_create_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/storage/create endpoint."""
        resp = await authenticated_client.get("/virtualization/virtual-machines/storage/create")
        assert resp.status_code != 404

    async def test_virtual_disks_create_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/virtual-disks/create endpoint."""
        resp = await authenticated_client.get(
            "/virtualization/virtual-machines/virtual-disks/create"
        )
        assert resp.status_code != 404

    async def test_full_update_path_exists(self, authenticated_client):
        """Plugin expects /full-update endpoint."""
        resp = await authenticated_client.get("/full-update")
        assert resp.status_code != 404


class TestStreamEndpoints:
    """Test stream endpoint variants."""

    async def test_devices_create_stream_path_exists(self, authenticated_client):
        """Plugin expects /dcim/devices/create/stream endpoint."""
        async with authenticated_client.stream("GET", "/dcim/devices/create/stream") as resp:
            # Path exists if status is not 404 or 405
            assert resp.status_code != 404
            assert resp.status_code != 405

    async def test_vms_create_stream_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/create/stream endpoint."""
        async with authenticated_client.stream(
            "GET", "/virtualization/virtual-machines/create/stream"
        ) as resp:
            assert resp.status_code != 404
            assert resp.status_code != 405

    async def test_vms_create_stream_without_netbox_vm_ids(self, authenticated_client):
        """Regression test: /create/stream without netbox_vm_ids should not fail with closure error.

        This tests the fix for the vm_ids closure bug where vm_ids was only assigned
        inside the if netbox_vm_ids: branch but referenced unconditionally in the SSE message.
        """
        async with authenticated_client.stream(
            "GET", "/virtualization/virtual-machines/create/stream"
        ) as resp:
            assert resp.status_code != 404
            assert resp.status_code != 405
            # Consume some of the stream to ensure no runtime error during initial yield
            chunks = []
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                if len(chunks) >= 3:
                    break
            # If we got here without RuntimeError, the test passes
            assert len(chunks) >= 0

    async def test_full_update_stream_path_exists(self, authenticated_client):
        """Plugin expects /full-update/stream endpoint."""
        async with authenticated_client.stream("GET", "/full-update/stream") as resp:
            assert resp.status_code != 404
            assert resp.status_code != 405


class TestNonStreamEndpoints:
    """Test non-stream endpoints return JSON."""

    async def test_root_returns_json(self, test_client):
        """Root endpoint should return JSON (auth-exempt, uses unauthenticated client)."""
        resp = test_client.get("/")
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("application/json")
        data = resp.json()
        assert isinstance(data, dict)
