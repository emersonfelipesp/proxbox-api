"""Tests for the Azure VHD import planning route."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.routes.cloud import azure_vhd_imports, azure_vhd_pipeline
from proxbox_api.schemas.cloud_provision import (
    AzureVhdImportRequest,
    CloudImageTemplateExecutionSummary,
    PackerFinding,
    PackerFindingSeverity,
)

PUBLIC_VHD_URL = "https://93.184.216.34/exported-osdisk.vhd"


def _request(**overrides: object) -> AzureVhdImportRequest:
    values: dict[str, object] = {
        "endpoint_id": 7,
        "target_node": "pve-node-01",
        "vmid": 9401,
        "name": "azure-linux-migrated",
        "azure_vhd_url": PUBLIC_VHD_URL,
        "execute": True,
    }
    values.update(overrides)
    return AzureVhdImportRequest(**values)


def _endpoint(**overrides: object) -> ProxmoxEndpoint:
    values: dict[str, object] = {
        "id": 7,
        "name": "azure-import-target",
        "ip_address": "192.0.2.10",
        "port": 8006,
        "username": "root@pam",
        "password": "secret",
        "enabled": True,
        "allow_writes": True,
        "access_methods": "api_ssh",
        "ssh_target_node": "pve-node-01",
        "ssh_host": "pve.example.test",
        "ssh_username": "root",
        "ssh_port": 2222,
        "ssh_identity_file": "/etc/proxbox/ssh_keys/id_ed25519",
        "ssh_known_host_fingerprint": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    }
    values.update(overrides)
    return ProxmoxEndpoint(**values)


def test_azure_vhd_import_route_returns_linux_plan(auth_test_client) -> None:
    response = auth_test_client.post(
        "/cloud/azure/vhd-imports",
        json={
            "target_node": "pve-node-01",
            "vmid": 9401,
            "name": "azure-linux-migrated",
            "azure_vhd_url": PUBLIC_VHD_URL,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["pipeline_name"] == "Azure VHD Import Pipeline"
    assert body["status"] == "planned"
    assert body["disk_interface"] == "scsi0"
    assert body["network_model"] == "virtio"
    assert body["bios"] == "ovmf"
    assert body["qcow2_filename"] == "exported-osdisk.qcow2"
    assert 'test "$(hostname -s)" = pve-node-01' in body["build_script"]
    assert "! qm status 9401 >/dev/null 2>&1" in body["build_script"]
    assert "pvesm status --storage local-zfs >/dev/null" in body["build_script"]
    assert "curl -fL --retry 3 -C -" in body["build_script"]
    assert (
        "qemu-img info /var/lib/vz/template/cache/exported-osdisk.vhd >/dev/null"
        in body["build_script"]
    )
    assert "qemu-img convert -f vpc -O qcow2" in body["build_script"]
    assert "IMPORT_OUTPUT=$(qm importdisk 9401" in body["build_script"]
    assert "Successfully imported disk as" in body["build_script"]
    assert "pvesm list" not in body["build_script"]
    assert "--scsihw virtio-scsi-single" in body["build_script"]
    assert "--boot order=scsi0" in body["build_script"]


def test_azure_vhd_import_route_returns_windows_safe_boot_plan(auth_test_client) -> None:
    response = auth_test_client.post(
        "/cloud/azure/vhd-imports",
        json={
            "target_node": "pve-node-02",
            "vmid": 9402,
            "name": "azure-windows-migrated",
            "azure_vhd_url": PUBLIC_VHD_URL,
            "guest_profile": "windows_first_boot_safe",
            "vm_generation": "gen1",
            "bridge": "vmbr1",
            "vlan_tag": 111,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["disk_interface"] == "sata0"
    assert body["network_model"] == "e1000"
    assert body["bios"] == "seabios"
    assert body["machine"] is None
    assert "virtio,bridge=" not in body["build_script"]
    assert "--net0 e1000,bridge=vmbr1,tag=111" in body["build_script"]
    assert "--sata0 " in body["build_script"]
    assert "--boot order=sata0" in body["build_script"]
    assert any("Install VirtIO storage" in step for step in body["follow_up_steps"])


def _create_proxmox_endpoint(client, *, name, ip, allow_writes, access_methods) -> int:
    resp = client.post(
        "/proxmox/endpoints",
        json={
            "name": name,
            "ip_address": ip,
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
            "allow_writes": allow_writes,
            "access_methods": access_methods,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_azure_vhd_import_execute_blocked_when_ssh_not_enabled(auth_test_client) -> None:
    """execute=true against an API-only endpoint is refused by the SSH gate."""
    endpoint_id = _create_proxmox_endpoint(
        auth_test_client,
        name="azure-api-only",
        ip="192.168.1.200",
        allow_writes=True,  # passes the allow_writes gate so the SSH gate is reached
        access_methods="api",  # API only -> SSH refused
    )

    response = auth_test_client.post(
        "/cloud/azure/vhd-imports",
        json={
            "target_node": "pve-node-01",
            "vmid": 9405,
            "name": "azure-ssh-blocked",
            "azure_vhd_url": PUBLIC_VHD_URL,
            "execute": True,
            "endpoint_id": endpoint_id,
            "ssh_host": "pve.example.test",
        },
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["reason"] == "ssh_not_enabled_for_endpoint"


def test_azure_vhd_import_execute_requires_endpoint_id(auth_test_client) -> None:
    response = auth_test_client.post(
        "/cloud/azure/vhd-imports",
        json={
            "target_node": "pve-node-01",
            "vmid": 9403,
            "name": "azure-execute",
            "azure_vhd_url": PUBLIC_VHD_URL,
            "execute": True,
            "ssh_host": "pve.example.test",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "endpoint_id is required when execute=true."


def test_azure_vhd_import_rejects_internal_urls() -> None:
    from pydantic import ValidationError

    from proxbox_api.schemas.cloud_provision import AzureVhdImportRequest

    try:
        AzureVhdImportRequest(
            target_node="pve-node-01",
            vmid=9404,
            name="azure-invalid-url",
            azure_vhd_url="http://127.0.0.1/exported.vhd",
        )
    except ValidationError as exc:
        assert "azure_vhd_url rejected by SSRF protection" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected SSRF validation failure for loopback URL")


@pytest.mark.asyncio
async def test_azure_execution_uses_safe_shared_executor_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorized = False

    async def authorize() -> None:
        nonlocal authorized
        authorized = True

    async def fake_execute(*_args: object, **kwargs: object) -> tuple[object, ...]:
        await kwargs["authorize_execution"]()
        assert str(kwargs["remote_unit"]).startswith("proxbox-azure-vhd-")
        return (
            "verification_pending",
            0,
            CloudImageTemplateExecutionSummary(
                attempted=True,
                enabled=True,
                stdout_bytes=10_000_000,
                stderr_bytes=20_000_000,
                stdout_lines=300_000,
                stderr_lines=400_000,
            ),
            [
                PackerFinding(
                    code="execution_awaiting_verification",
                    severity=PackerFindingSeverity.warning,
                    target="endpoint:7",
                    message="Remote execution completed.",
                )
            ],
            None,
        )

    monkeypatch.setenv("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "true")
    monkeypatch.setattr(azure_vhd_pipeline, "execute_remote_script", fake_execute)
    target = azure_vhd_imports._resolve_target(_endpoint(), _request())
    response = await azure_vhd_pipeline.build_azure_vhd_import_response(
        _request(azure_vhd_url="https://93.184.216.34/exported.vhd?sig=SECRET-CANARY"),
        execution_target=target,
        authorize_execution=authorize,
    )

    rendered = response.model_dump_json()
    assert authorized
    assert response.azure_vhd_url == ""
    assert response.build_script == ""
    assert response.commands == []
    assert response.stdout is None and response.stderr is None
    assert response.execution.stdout_bytes == 10_000_000
    assert "SECRET-CANARY" not in rendered


@pytest.mark.asyncio
async def test_azure_execution_rejects_caller_binding_mismatch_before_resources_open() -> None:
    with pytest.raises(HTTPException) as exc_info:
        azure_vhd_imports._resolve_target(_endpoint(), _request(ssh_host="other.example.test"))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "endpoint_ssh_binding_mismatch"


@pytest.mark.asyncio
async def test_azure_execution_requires_persisted_host_fingerprint() -> None:
    with pytest.raises(HTTPException) as exc_info:
        azure_vhd_imports._resolve_target(_endpoint(ssh_known_host_fingerprint=None), _request())

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "endpoint_ssh_binding_incomplete"


@pytest.mark.asyncio
async def test_azure_authority_refresh_reloads_identity_map_and_rejects_revocation() -> None:
    endpoint = _endpoint()

    class Session:
        async def get(self, _model: object, _endpoint_id: int) -> ProxmoxEndpoint:
            return endpoint

        async def refresh(self, value: ProxmoxEndpoint) -> None:
            value.allow_writes = False

    refreshed = await azure_vhd_imports._refresh_endpoint(Session(), 7)
    with pytest.raises(HTTPException) as exc_info:
        azure_vhd_imports._resolve_target(refreshed, _request())

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "endpoint_writes_disabled"


@pytest.mark.asyncio
async def test_azure_refresh_failure_never_starts_or_cancels_ssh(monkeypatch) -> None:
    calls: list[str] = []

    class Session:
        async def get(self, _model: object, _endpoint_id: int) -> ProxmoxEndpoint:
            return _endpoint()

        async def refresh(self, _value: ProxmoxEndpoint) -> None:
            raise RuntimeError("SECRET-DATABASE-CANARY")

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        calls.append("ssh")

    monkeypatch.setattr(
        "proxbox_api.routes.cloud.pipeline_scripts.asyncio.create_subprocess_exec",
        forbidden,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.cloud.pipeline_scripts._cancel_remote_unit",
        forbidden,
    )
    with pytest.raises(HTTPException) as exc_info:
        await azure_vhd_imports._refresh_endpoint(Session(), 7)

    assert exc_info.value.detail["code"] == "endpoint_authority_refresh_failed"
    assert "SECRET-DATABASE-CANARY" not in str(exc_info.value.detail)
    assert calls == []


@pytest.mark.parametrize(
    ("error_code", "attempted", "cancel_attempted", "cancel_succeeded", "expected"),
    [
        ("ssh_identity_untrusted", False, False, None, False),
        ("ssh_host_key_unverified", True, False, None, False),
        ("execution_unavailable", True, False, None, False),
        ("execution_timeout", True, True, True, False),
        ("execution_timeout", True, True, False, True),
        ("execution_failed", True, False, None, True),
    ],
)
def test_azure_recovery_truth_table(
    error_code: str,
    attempted: bool,
    cancel_attempted: bool,
    cancel_succeeded: bool | None,
    expected: bool,
) -> None:
    summary = CloudImageTemplateExecutionSummary(
        attempted=attempted,
        cancellation_attempted=cancel_attempted,
        cancellation_succeeded=cancel_succeeded,
    )
    assert azure_vhd_pipeline._recovery_required(error_code, summary) is expected


@pytest.mark.asyncio
async def test_azure_restores_cancellation_after_shared_cleanup(monkeypatch) -> None:
    cleanup = CloudImageTemplateExecutionSummary(
        attempted=True,
        cancellation_attempted=True,
        cancellation_succeeded=True,
    )

    async def cancelled(*_args: object, **_kwargs: object) -> None:
        raise azure_vhd_pipeline.PipelineExecutionCancelled(cleanup)

    monkeypatch.setattr(azure_vhd_pipeline, "execute_remote_script", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await azure_vhd_pipeline._execution_result(
            _request(),
            azure_vhd_imports._resolve_target(_endpoint(), _request()),
            "true\n",
            execution_allowed=True,
            authorize_execution=None,
        )
