"""Cloud provisioning lifecycle tests against the schema-driven Proxmox mock.

The current mock records config mutations and exposes generated status payloads,
but clone itself is schema-generic rather than a full Proxmox task emulator.
These tests still exercise the FastAPI route, write gate, mock-backed QEMU
config update, cloudinit drive attachment, and start dispatch without a live
cluster.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session

from proxbox_api.database import ProxmoxEndpoint, get_async_session
from proxbox_api.main import app
from proxbox_api.testing.proxmox_mock import reset_mock_state


def _make_endpoint(db_session: Session, *, allow_writes: bool) -> int:
    endpoint = ProxmoxEndpoint(
        name=f"pve-cloud-{allow_writes}",
        ip_address="10.0.0.10",
        port=8006,
        username="root@pam",
        verify_ssl=False,
        allow_writes=allow_writes,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    assert endpoint.id is not None
    return endpoint.id


@pytest.fixture
def sync_async_db_override(db_engine):
    async def _override_get_async_session():
        with Session(db_engine) as session:
            yield session

    app.dependency_overrides[get_async_session] = _override_get_async_session
    yield
    app.dependency_overrides.pop(get_async_session, None)


async def _seed_template() -> None:
    from proxmox_sdk import ProxmoxSDK

    sdk = ProxmoxSDK(host="mock", backend="mock", verify_ssl=False)
    try:
        await sdk.nodes.post(node="pve", type="node", status="online")
        await sdk.nodes("pve").qemu.post(
            vmid=9000,
            name="ubuntu-cloud-template",
            ostype="l26",
            template=1,
        )
    finally:
        await sdk.close()


async def _read_mock_vm_state() -> tuple[dict[str, object], dict[str, object]]:
    from proxmox_sdk import ProxmoxSDK

    sdk = ProxmoxSDK(host="mock", backend="mock", verify_ssl=False)
    try:
        config = await sdk.nodes("pve").qemu(9100).config.get()
        status = await sdk.nodes("pve").qemu(9100).status.current.get()
        return dict(config), dict(status)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_cloud_provision_clones_configures_cloudinit_and_starts(
    auth_headers,
    db_session,
    monkeypatch,
    sync_async_db_override,
):
    monkeypatch.setenv("PROXMOX_API_MODE", "mock")
    reset_mock_state()
    endpoint_id = _make_endpoint(db_session, allow_writes=True)
    await _seed_template()

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=auth_headers,
        ) as client:
            response = await client.post(
                "/cloud/vm/provision",
                json={
                    "endpoint_id": endpoint_id,
                    "template_vmid": 9000,
                    "new_vmid": 9100,
                    "new_name": "tenant-vm-9100",
                    "target_node": "pve",
                    "cloud_init": {
                        "user": "ubuntu",
                        "ssh_keys": ["ssh-rsa AAA"],
                    },
                    "start_after_provision": True,
                },
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["new_vmid"] == 9100
        assert body["status"] == "started"
        assert body["start_upid"] is not None

        config, status_payload = await _read_mock_vm_state()
        assert "cloudinit" in str(config.get("ide2"))
        assert config["ciuser"] == "ubuntu"
        assert status_payload["status"] == "running"
    finally:
        reset_mock_state()


@pytest.mark.asyncio
async def test_cloud_provision_writes_disabled_returns_gate_reason(
    auth_headers,
    db_session,
    sync_async_db_override,
):
    endpoint_id = _make_endpoint(db_session, allow_writes=False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        response = await client.post(
            "/cloud/vm/provision",
            json={
                "endpoint_id": endpoint_id,
                "template_vmid": 9000,
                "new_vmid": 9100,
                "new_name": "tenant-vm-9100",
                "target_node": "pve",
                "cloud_init": {"ssh_keys": ["ssh-rsa AAA"]},
            },
        )

    assert response.status_code == 403
    assert response.json()["reason"] == "endpoint_writes_disabled"
