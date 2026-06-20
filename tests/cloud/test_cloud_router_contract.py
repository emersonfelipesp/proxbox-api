"""Static and registration checks for Cloud Portal routes."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from proxbox_api.app import factory
from proxbox_api.routes import cloud
from proxbox_api.routes.cloud import provision as provision_route
from proxbox_api.routes.intent.cloud_init import CloudInitPayload
from proxbox_api.schemas.cloud_provision import CloudVMProvisionRequest


def _join_route_path(prefix: str, path: str | None) -> str:
    if not path or path == "/":
        return prefix or "/"
    if not prefix:
        return path
    return f"{prefix.rstrip('/')}/{path.lstrip('/')}"


def _iter_registered_routes(
    routes: Iterable[object],
    prefix: str = "",
) -> Iterator[tuple[str, set[str]]]:
    for route in routes:
        include_context = getattr(route, "include_context", None)
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            include_prefix = getattr(include_context, "prefix", "") if include_context else ""
            yield from _iter_registered_routes(
                getattr(original_router, "routes", ()),
                _join_route_path(prefix, include_prefix),
            )
            continue

        nested_routes = getattr(route, "routes", None)
        if nested_routes:
            yield from _iter_registered_routes(
                nested_routes,
                _join_route_path(prefix, getattr(route, "path", None)),
            )
            continue

        path = getattr(route, "path", None)
        if path is not None:
            yield _join_route_path(prefix, path), set(getattr(route, "methods", None) or ())


def test_cloud_package_exposes_both_routers():
    assert cloud.azure_vhd_imports_router is not None
    assert cloud.lxc_router is not None
    assert cloud.provision_router is not None
    assert cloud.provision_stream_router is not None
    assert cloud.firecracker_router is not None
    assert cloud.image_factory_router is not None
    assert cloud.template_images_router is not None
    assert cloud.templates_router is not None
    assert cloud.pve_template_router is not None
    assert cloud.qemu_templates_router is not None
    assert cloud.versions_router is not None
    assert cloud.__all__ == (
        "azure_vhd_imports_router",
        "lxc_router",
        "provision_router",
        "provision_stream_router",
        "firecracker_router",
        "image_factory_router",
        "pve_template_router",
        "qemu_templates_router",
        "template_images_router",
        "templates_router",
        "versions_router",
    )


def test_cloud_routes_are_registered_on_app(monkeypatch):
    monkeypatch.delenv("PROXBOX_FEATURES", raising=False)

    test_app = factory.create_app()
    routes = list(_iter_registered_routes(test_app.routes))

    assert any(
        path == "/cloud/vm/provision" and "POST" in methods for path, methods in routes
    )
    assert any(
        path == "/cloud/templates" and "GET" in methods for path, methods in routes
    )
    assert any(
        path == "/cloud/templates/images" and "POST" in methods for path, methods in routes
    )
    assert any(
        path == "/cloud/vm/templates" and "GET" in methods for path, methods in routes
    )


def _valid_request_payload() -> dict[str, object]:
    return {
        "endpoint_id": 1,
        "template_vmid": 9000,
        "new_vmid": 9100,
        "new_name": "tenant-vm-9100",
        "target_node": "pve",
        "cloud_init": CloudInitPayload(user="ubuntu", ssh_keys=["ssh-rsa AAA"]),
    }


def test_cloud_provision_request_rejects_extra_fields():
    payload = _valid_request_payload()
    payload["unexpected"] = "blocked"

    with pytest.raises(ValidationError):
        CloudVMProvisionRequest.model_validate(payload)


def test_cloud_provision_request_rejects_template_vmid_below_100():
    payload = _valid_request_payload()
    payload["template_vmid"] = 99

    with pytest.raises(ValidationError):
        CloudVMProvisionRequest.model_validate(payload)


def test_cloud_provision_request_accepts_qemu_topology_and_vlan_fields():
    payload = _valid_request_payload()
    payload.update(
        {
            "memory_mb": 16384,
            "cores": 4,
            "sockets": 2,
            "disk_gb": 200,
            "bridge": "vmbr1",
            "vlan_tag": 23,
        }
    )

    request = CloudVMProvisionRequest.model_validate(payload)

    assert request.memory_mb == 16384
    assert request.cores == 4
    assert request.sockets == 2
    assert request.disk_gb == 200
    assert request.bridge == "vmbr1"
    assert request.vlan_tag == 23


def test_cloud_provision_request_rejects_invalid_vlan_tag():
    payload = _valid_request_payload()
    payload["vlan_tag"] = 4095

    with pytest.raises(ValidationError):
        CloudVMProvisionRequest.model_validate(payload)


def test_net0_override_preserves_mac_and_replaces_bridge_tag():
    net0 = provision_route._build_net0_override(
        {"net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,firewall=1,tag=7"},
        bridge="vmbr1",
        vlan_tag=23,
    )

    assert net0 == "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr1,firewall=1,tag=23"


@pytest.mark.asyncio
async def test_configure_cloud_init_vm_forwards_topology_and_network(monkeypatch):
    captured: dict[str, object] = {}

    class _Config:
        async def put(self, **kwargs):
            captured.update(kwargs)
            return {"data": "UPID:pve:config"}

    class _Qemu:
        config = _Config()

    class _Node:
        def qemu(self, _vmid):
            return _Qemu()

    class _Session:
        def nodes(self, _node):
            return _Node()

    class _Proxmox:
        session = _Session()

    async def _existing_config(*_args, **_kwargs):
        return {
            "ide2": "local-lvm:cloudinit",
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,firewall=1",
        }

    monkeypatch.setattr(provision_route, "_get_qemu_config_best_effort", _existing_config)

    upid = await provision_route._configure_cloud_init_vm(
        _Proxmox(),
        CloudVMProvisionRequest(
            endpoint_id=1,
            template_vmid=9000,
            new_vmid=9100,
            new_name="tenant-vm-9100",
            target_node="pve",
            cloud_init=CloudInitPayload(user="root", network={"ip": "10.0.23.1/24"}),
            memory_mb=16384,
            cores=4,
            sockets=2,
            bridge="vmbr1",
            vlan_tag=23,
        ),
    )

    assert upid == "UPID:pve:config"
    assert captured["memory"] == 16384
    assert captured["cores"] == 4
    assert captured["sockets"] == 2
    assert captured["ciuser"] == "root"
    assert captured["ipconfig0"] == "ip=10.0.23.1/24"
    assert captured["net0"] == "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr1,firewall=1,tag=23"


@pytest.mark.asyncio
async def test_resize_vm_disk_uses_scsi0_and_requested_size():
    captured: dict[str, object] = {}

    class _Resize:
        async def put(self, **kwargs):
            captured.update(kwargs)
            return {"data": "UPID:pve:resize"}

    class _Qemu:
        resize = _Resize()

    class _Node:
        def qemu(self, _vmid):
            return _Qemu()

    class _Session:
        def nodes(self, _node):
            return _Node()

    class _Proxmox:
        session = _Session()

    upid = await provision_route._resize_vm_disk(
        _Proxmox(),
        CloudVMProvisionRequest(
            endpoint_id=1,
            template_vmid=9000,
            new_vmid=9100,
            new_name="tenant-vm-9100",
            target_node="pve",
            cloud_init=CloudInitPayload(user="root"),
            disk_gb=200,
        ),
    )

    assert upid == "UPID:pve:resize"
    assert captured == {"disk": "scsi0", "size": "200G"}


def test_cloud_provision_route_reuses_required_helpers():
    source = (
        Path(__file__).parents[2] / "proxbox_api" / "routes" / "cloud" / "provision.py"
    ).read_text(encoding="utf-8")

    assert "build_proxmox_ci_args" in source
    assert "_gate" in source
    assert "_wait_for_upid" in source
    assert "await _wait_for_upid(proxmox, template_node, clone_upid)" in source
