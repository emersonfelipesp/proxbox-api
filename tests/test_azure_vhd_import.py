"""Tests for the Azure VHD import planning route."""

from __future__ import annotations

PUBLIC_VHD_URL = "https://93.184.216.34/exported-osdisk.vhd"


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
