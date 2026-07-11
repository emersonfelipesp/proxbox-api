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
from proxbox_api.routes.cloud import provision as provision_route
from proxbox_api.routes.intent.cloud_init import CloudInitPayload
from proxbox_api.schemas.cloud_provision import CloudVMProvisionRequest
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


def test_build_agent_override_merges_existing_options():
    from proxbox_api.routes.cloud.provision import _build_agent_override

    assert _build_agent_override(None) == "enabled=1"
    assert _build_agent_override({}) == "enabled=1"
    assert _build_agent_override({"agent": "1"}) == "enabled=1"
    # Preserve sub-options while forcing the agent on (don't clobber the field).
    assert (
        _build_agent_override({"agent": "fstrim_cloned_disks=1,type=virtio"})
        == "enabled=1,fstrim_cloned_disks=1,type=virtio"
    )
    assert _build_agent_override({"agent": "enabled=0,type=virtio"}) == "enabled=1,type=virtio"
    # #222: normalized through mapping_from_response, so a {"data": ...} envelope
    # (or a non-dict) is handled defensively instead of raising AttributeError.
    assert _build_agent_override({"data": {"agent": "type=virtio"}}) == "enabled=1,type=virtio"
    assert _build_agent_override("not-a-dict") == "enabled=1"  # type: ignore[arg-type]


def test_cloud_init_password_is_length_bounded() -> None:
    """#222: CloudInitPayload.password must be bounded (max 128) so an oversized
    cipassword is rejected client-side rather than bloating the VM config or
    producing an opaque Proxmox error."""
    import pytest as _pytest
    from pydantic import ValidationError

    CloudInitPayload(password="x" * 128)  # ok at the boundary
    with _pytest.raises(ValidationError):
        CloudInitPayload(password="x" * 129)


@pytest.mark.asyncio
async def test_step_rollback_scrubs_cipassword_even_without_lease() -> None:
    """#222: the default enforce_cloud_network=False path (lease is None) must
    still route exceptions through _proxmox_step_failed so the cipassword is
    redacted from the client 502 body — parity with the SSE stream."""
    from fastapi import HTTPException

    async def _boom() -> None:
        raise RuntimeError("proxmox rejected cipassword=SuperSecret123 on config.put")

    with pytest.raises(HTTPException) as exc:
        await provision_route._run_proxmox_step_with_cloud_network_rollback(
            "configure_cloud_init", _boom(), None
        )
    assert exc.value.status_code == 502
    assert "SuperSecret123" not in repr(exc.value.detail)


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
        # enable_agent defaults True → every clone forces the guest agent on.
        assert "enabled=1" in str(config.get("agent"))
    finally:
        reset_mock_state()


@pytest.mark.asyncio
async def test_cloud_provision_sets_cipassword_and_can_disable_agent(
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
                        "password": "s3cret-pw",
                        "ssh_keys": ["ssh-rsa AAA"],
                    },
                    "enable_agent": False,
                    "start_after_provision": False,
                },
            )

        assert response.status_code == 200, response.text
        config, _ = await _read_mock_vm_state()
        # Req 2: username+password SSH — password reaches Proxmox cipassword.
        assert config["cipassword"] == "s3cret-pw"
        # enable_agent=False explicitly opts out, so no agent flag is written.
        assert "agent" not in config
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


@pytest.mark.asyncio
async def test_cloud_provision_gate_refreshes_allow_writes_from_netbox(
    db_session,
    monkeypatch,
):
    """Cloud provisioning should honor current NetBox allow_writes for a stale local row."""
    endpoint_id = _make_endpoint(db_session, allow_writes=False)

    async def fake_get_netbox_async_session(*, database_session):
        assert database_session is db_session
        return object()

    async def fake_rest_list_async(_nb, path, query=None):
        assert path == "/api/plugins/proxbox/endpoints/proxmox/"
        assert query == {"limit": 0}
        return [
            {
                "name": "pve-cloud-False",
                "ip_address": {"address": "10.0.0.10/32"},
                "allow_writes": True,
            }
        ]

    monkeypatch.setattr(provision_route, "get_netbox_async_session", fake_get_netbox_async_session)
    monkeypatch.setattr(provision_route, "rest_list_async", fake_rest_list_async)

    gated = await provision_route._cloud_provision_gate(db_session, endpoint_id)

    assert not hasattr(gated, "status_code")
    db_session.refresh(gated)
    assert gated.allow_writes is True


@pytest.mark.asyncio
async def test_cloud_provision_gate_preserves_refusal_when_netbox_writes_disabled(
    db_session,
    monkeypatch,
):
    """The NetBox confirmation path must preserve deny-by-default behavior."""
    endpoint_id = _make_endpoint(db_session, allow_writes=False)

    async def fake_get_netbox_async_session(*, database_session):
        assert database_session is db_session
        return object()

    async def fake_rest_list_async(_nb, path, query=None):
        assert path == "/api/plugins/proxbox/endpoints/proxmox/"
        return [
            {
                "name": "pve-cloud-False",
                "ip_address": {"address": "10.0.0.10/32"},
                "allow_writes": False,
            }
        ]

    monkeypatch.setattr(provision_route, "get_netbox_async_session", fake_get_netbox_async_session)
    monkeypatch.setattr(provision_route, "rest_list_async", fake_rest_list_async)

    gated = await provision_route._cloud_provision_gate(db_session, endpoint_id)

    assert getattr(gated, "status_code", None) == 403


@pytest.mark.asyncio
async def test_cloud_provision_waits_for_config_upid_before_start(monkeypatch) -> None:
    events: list[str] = []

    class _Proxmox:
        async def aclose(self) -> None:
            events.append("close")

    async def _gate(_session, _endpoint_id):
        events.append("gate")
        return object()

    async def _open(_endpoint):
        events.append("open")
        return _Proxmox()

    async def _clone(_proxmox, _req):
        events.append("clone")
        return "clone-result"

    async def _configure(_proxmox, _req):
        events.append("configure")
        return "UPID:pve:config"

    async def _wait(_proxmox, node, upid):
        events.append(f"wait:{node}:{upid}")

    async def _start(_proxmox, _req):
        events.append("start")
        return "start-result"

    async def _journal(**_kwargs):
        events.append("journal")

    monkeypatch.setattr(provision_route, "_gate", _gate)
    monkeypatch.setattr(provision_route, "_open_proxmox_session", _open)
    monkeypatch.setattr(provision_route, "_clone_template_vm", _clone)
    monkeypatch.setattr(provision_route, "_configure_cloud_init_vm", _configure)
    monkeypatch.setattr(provision_route, "_wait_for_upid", _wait)
    monkeypatch.setattr(provision_route, "_start_vm_after_provision", _start)
    monkeypatch.setattr(provision_route, "_journal_provision_best_effort", _journal)
    monkeypatch.setattr(provision_route, "_should_wait_for_upid", lambda: True)

    response = await provision_route.provision_vm(
        CloudVMProvisionRequest(
            endpoint_id=1,
            template_vmid=9000,
            new_vmid=9100,
            new_name="tenant-vm-9100",
            target_node="pve",
            cloud_init=CloudInitPayload(user="ubuntu", ssh_keys=["ssh-rsa AAA"]),
        ),
        session=object(),
    )

    assert response.config_upid == "UPID:pve:config"
    assert events == [
        "gate",
        "open",
        "clone",
        "configure",
        "wait:pve:UPID:pve:config",
        "start",
        "journal",
        "close",
    ]
