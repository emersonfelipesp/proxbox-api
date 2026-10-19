"""Static and registration checks for Cloud Portal routes."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from proxbox_api.app import factory
from proxbox_api.routes import cloud
from proxbox_api.routes.cloud import provision as provision_route
from proxbox_api.routes.cloud import provision_stream as provision_stream_route
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
    assert cloud.network_router is not None
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
        "network_router",
        "qemu_templates_router",
        "template_images_router",
        "templates_router",
        "versions_router",
    )
    assert cloud.azure_vhd_imports_router is not None
    assert cloud.lxc_router is not None


def test_cloud_routes_are_registered_on_app(monkeypatch):
    monkeypatch.delenv("PROXBOX_FEATURES", raising=False)

    test_app = factory.create_app()
    routes = list(_iter_registered_routes(test_app.routes))

    assert any(path == "/cloud/vm/provision" and "POST" in methods for path, methods in routes)
    assert any(path == "/cloud/templates" and "GET" in methods for path, methods in routes)
    assert any(path == "/cloud/templates/images" and "POST" in methods for path, methods in routes)
    assert any(path == "/cloud/vm/templates" and "GET" in methods for path, methods in routes)
    assert any(
        path == "/cloud/network/available-ips" and "GET" in methods for path, methods in routes
    )


def test_cloud_provision_openapi_exposes_extra_disks_and_metadata(monkeypatch):
    monkeypatch.delenv("PROXBOX_FEATURES", raising=False)

    schema = factory.create_app().openapi()
    request_schema = schema["components"]["schemas"]["CloudVMProvisionRequest"]
    properties = request_schema["properties"]

    assert {"extra_disks", "tags", "description"} <= properties.keys()
    assert properties["extra_disks"]["maxItems"] == 8
    assert properties["tags"]["maxItems"] == 16
    assert properties["description"]["anyOf"][0]["maxLength"] == 512
    for path in ("/cloud/vm/provision", "/cloud/vm/provision/stream"):
        request_body = schema["paths"][path]["post"]["requestBody"]
        assert request_body["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/CloudVMProvisionRequest"
        }


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
            "enforce_cloud_network": True,
        }
    )

    request = CloudVMProvisionRequest.model_validate(payload)

    assert request.memory_mb == 16384
    assert request.cores == 4
    assert request.sockets == 2
    assert request.disk_gb == 200
    assert request.bridge == "vmbr1"
    assert request.vlan_tag == 23
    assert request.enforce_cloud_network is True


def test_cloud_provision_request_rejects_invalid_vlan_tag():
    payload = _valid_request_payload()
    payload["vlan_tag"] = 4095

    with pytest.raises(ValidationError):
        CloudVMProvisionRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("bus", "index"),
    [("scsi", 0), ("scsi", 31), ("virtio", 0), ("virtio", 16), ("sata", 0), ("sata", 6)],
)
def test_cloud_provision_request_rejects_invalid_extra_disk_slots(bus, index):
    payload = _valid_request_payload()
    payload["extra_disks"] = [{"bus": bus, "index": index, "size_gb": 1}]

    with pytest.raises(ValidationError):
        CloudVMProvisionRequest.model_validate(payload)


def test_cloud_provision_request_rejects_duplicate_extra_disk_slots():
    payload = _valid_request_payload()
    payload["extra_disks"] = [
        {"bus": "scsi", "index": 1, "size_gb": 10},
        {"bus": "scsi", "index": 1, "size_gb": 20},
    ]

    with pytest.raises(ValidationError):
        CloudVMProvisionRequest.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("extra_disks", [{"bus": "scsi", "index": index, "size_gb": 1} for index in range(1, 10)]),
        ("tags", [f"tag{index}" for index in range(17)]),
        ("tags", ["invalid tag"]),
        ("description", "invalid\tcontrol"),
        ("description", "invalid\u0085control"),
    ],
)
def test_cloud_provision_request_rejects_invalid_metadata(field, value):
    payload = _valid_request_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        CloudVMProvisionRequest.model_validate(payload)


def test_build_extra_disk_args_resolves_storage_and_options():
    request = CloudVMProvisionRequest.model_validate(
        {
            **_valid_request_payload(),
            "storage": "request-store",
            "extra_disks": [
                {
                    "bus": "scsi",
                    "index": 1,
                    "storage": "disk-store",
                    "size_gb": 100,
                    "discard": True,
                    "ssd": True,
                },
                {"bus": "virtio", "index": 2, "size_gb": 20},
            ],
        }
    )

    assert provision_route.build_extra_disk_args(request, {}, "clone-store") == {
        "scsi1": "disk-store:100,discard=on,ssd=1",
        "virtio2": "request-store:20",
    }


def test_build_extra_disk_args_uses_inferred_clone_storage():
    request = CloudVMProvisionRequest.model_validate(
        {
            **_valid_request_payload(),
            "extra_disks": [{"bus": "sata", "index": 1, "size_gb": 32}],
        }
    )

    assert provision_route.build_extra_disk_args(request, {}, "clone-store") == {
        "sata1": "clone-store:32"
    }


def test_build_extra_disk_args_refuses_existing_slot():
    from fastapi import HTTPException

    request = CloudVMProvisionRequest.model_validate(
        {
            **_valid_request_payload(),
            "extra_disks": [{"bus": "scsi", "index": 1, "size_gb": 100}],
        }
    )

    with pytest.raises(HTTPException) as exc:
        provision_route.build_extra_disk_args(
            request,
            {"scsi1": "local-lvm:vm-9100-disk-1"},
            "local-lvm",
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == {"reason": "extra_disk_slot_in_use", "slot": "scsi1"}


@pytest.mark.parametrize(
    "storage",
    ["local-lvm:1,backup=0", "a=b", "a,b", "x y", "-lead", "", "s" * 65],
)
def test_build_extra_disk_args_refuses_storage_that_could_inject_options(storage):
    """The request-level storage is free text; an option separator must not reach Proxmox."""
    from fastapi import HTTPException

    request = CloudVMProvisionRequest.model_validate(
        {
            **_valid_request_payload(),
            "storage": storage or None,
            "extra_disks": [{"bus": "scsi", "index": 1, "size_gb": 100}],
        }
    )

    with pytest.raises(HTTPException) as exc:
        provision_route.build_extra_disk_args(request, {}, storage if storage else "bad:store")

    assert exc.value.status_code == 422
    assert exc.value.detail == {"reason": "extra_disk_storage_invalid", "slot": "scsi1"}


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"scsi0": "local-lvm:vm-9100-disk-0"}, "local-lvm"),
        (
            {
                "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
                "description": "Managed by: NetBox",
                "scsi0": "clone-store:vm-9100-disk-0,discard=on",
            },
            "clone-store",
        ),
        (
            {
                "ide2": "iso-store:iso/debian.iso,media=cdrom",
                "scsi2": "slow-store:vm-9100-disk-2",
                "virtio1": "fast-store:vm-9100-disk-1",
            },
            "fast-store",
        ),
        (
            {
                "ide2": "local-lvm:cloudinit",
                "scsi0": "none",
                "description": "Managed by: NetBox",
            },
            "local-lvm",
        ),
        ({"sata0": "sata-store:vm-9100-disk-0", "scsi0": "scsi-store:disk"}, "scsi-store"),
        (None, "local-lvm"),
    ],
)
def test_infer_default_storage_uses_attached_disks_only(config, expected):
    assert provision_route._infer_default_storage(config) == expected


@pytest.mark.parametrize("tag", ["-tenant", "+tenant", ".tenant", "", "a" * 65, "bad tag", "a;b"])
def test_tags_refuse_values_proxmox_rejects_after_the_clone(tag):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="tags must start with"):
        CloudVMProvisionRequest.model_validate({**_valid_request_payload(), "tags": [tag]})


@pytest.mark.parametrize("tag", ["_tenant", "9lab", "netbox-pbs", "a.b+c-d", "a" * 64])
def test_tags_accept_values_proxmox_accepts(tag):
    request = CloudVMProvisionRequest.model_validate({**_valid_request_payload(), "tags": [tag]})
    assert request.tags == [tag]


def test_extra_disk_refuses_ssd_on_virtio_before_any_allocation():
    """Proxmox rejects ssd on VirtIO block disks after the clone, so refuse it up front."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="ssd is not supported for virtio"):
        CloudVMProvisionRequest.model_validate(
            {
                **_valid_request_payload(),
                "extra_disks": [{"bus": "virtio", "index": 1, "size_gb": 10, "ssd": True}],
            }
        )
    for bus in ("scsi", "sata"):
        CloudVMProvisionRequest.model_validate(
            {
                **_valid_request_payload(),
                "extra_disks": [{"bus": bus, "index": 1, "size_gb": 10, "ssd": True}],
            }
        )
    CloudVMProvisionRequest.model_validate(
        {
            **_valid_request_payload(),
            "extra_disks": [{"bus": "virtio", "index": 1, "size_gb": 10, "discard": True}],
        }
    )


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
async def test_configure_cloud_init_vm_adds_disks_tags_and_description(monkeypatch):
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
        return {"scsi0": "clone-store:vm-9100-disk-0", "ide2": "clone-store:cloudinit"}

    monkeypatch.setattr(provision_route, "_get_qemu_config_best_effort", _existing_config)

    await provision_route._configure_cloud_init_vm(
        _Proxmox(),
        CloudVMProvisionRequest.model_validate(
            {
                **_valid_request_payload(),
                "extra_disks": [
                    {
                        "bus": "scsi",
                        "index": 1,
                        "size_gb": 100,
                        "discard": True,
                        "ssd": True,
                    }
                ],
                "tags": ["NetBox", "PBS-Server"],
                "description": "Managed by NetBox\nPBS datastore host",
            }
        ),
    )

    assert captured["scsi1"] == "clone-store:100,discard=on,ssd=1"
    assert captured["tags"] == "netbox;pbs-server"
    assert captured["description"] == "Managed by NetBox\nPBS datastore host"


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_config", ("not-a-mapping", ["scsi0"], 7))
async def test_configure_cloud_init_vm_refuses_malformed_config_for_extra_disks(
    monkeypatch, malformed_config
):
    put_called = False

    class _Config:
        async def put(self, **_kwargs):
            nonlocal put_called
            put_called = True

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
        return malformed_config

    monkeypatch.setattr(provision_route, "_get_qemu_config_best_effort", _existing_config)
    request = CloudVMProvisionRequest.model_validate(
        {
            **_valid_request_payload(),
            "extra_disks": [{"bus": "scsi", "index": 1, "size_gb": 100}],
        }
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await provision_route._configure_cloud_init_vm(_Proxmox(), request)

    assert exc.value.status_code == 502
    assert exc.value.detail == {"reason": "config_read_failed"}
    assert put_called is False


@pytest.mark.asyncio
async def test_configure_cloud_init_vm_refuses_config_timeout_for_extra_disks():
    put_called = False

    class _Config:
        async def get(self):
            raise TimeoutError

        async def put(self, **_kwargs):
            nonlocal put_called
            put_called = True

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

    request = CloudVMProvisionRequest.model_validate(
        {
            **_valid_request_payload(),
            "extra_disks": [{"bus": "scsi", "index": 1, "size_gb": 100}],
        }
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await provision_route._configure_cloud_init_vm(_Proxmox(), request)

    assert exc.value.status_code == 502
    assert exc.value.detail == {"reason": "config_read_failed"}
    assert put_called is False


@pytest.mark.asyncio
async def test_configure_cloud_init_vm_forwards_config_digest_with_extra_disks(monkeypatch):
    captured: dict[str, object] = {}

    class _Config:
        async def put(self, **kwargs):
            captured.update(kwargs)

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
            "digest": "sha256-config-digest",
            "scsi0": "clone-store:vm-9100-disk-0",
            "ide2": "clone-store:cloudinit",
        }

    monkeypatch.setattr(provision_route, "_get_qemu_config_best_effort", _existing_config)
    request = CloudVMProvisionRequest.model_validate(
        {
            **_valid_request_payload(),
            "extra_disks": [{"bus": "scsi", "index": 1, "size_gb": 100}],
        }
    )

    await provision_route._configure_cloud_init_vm(_Proxmox(), request)

    assert captured["digest"] == "sha256-config-digest"
    assert captured["scsi1"] == "clone-store:100"


@pytest.mark.asyncio
async def test_configure_cloud_init_vm_default_payload_is_unchanged(monkeypatch):
    captured: dict[str, object] = {}

    class _Config:
        async def put(self, **kwargs):
            captured.update(kwargs)
            return None

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
        return {"digest": "legacy-read-digest", "ide2": "local-lvm:cloudinit"}

    monkeypatch.setattr(provision_route, "_get_qemu_config_best_effort", _existing_config)

    await provision_route._configure_cloud_init_vm(
        _Proxmox(), CloudVMProvisionRequest.model_validate(_valid_request_payload())
    )

    assert captured == {
        "ciuser": "ubuntu",
        "sshkeys": "ssh-rsa%20AAA",
        "agent": "enabled=1",
    }


@pytest.mark.asyncio
async def test_configure_cloud_init_vm_refuses_collision_before_config_write(monkeypatch):
    put_called = False

    class _Config:
        async def put(self, **_kwargs):
            nonlocal put_called
            put_called = True

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
        return {"scsi1": "local-lvm:existing", "ide2": "local-lvm:cloudinit"}

    monkeypatch.setattr(provision_route, "_get_qemu_config_best_effort", _existing_config)
    request = CloudVMProvisionRequest.model_validate(
        {
            **_valid_request_payload(),
            "extra_disks": [{"bus": "scsi", "index": 1, "size_gb": 100}],
        }
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await provision_route._configure_cloud_init_vm(_Proxmox(), request)

    assert exc.value.status_code == 409
    assert put_called is False


@pytest.mark.asyncio
async def test_stream_configure_uses_shared_extra_disk_path(monkeypatch):
    captured: list[CloudVMProvisionRequest] = []

    async def _configure(_proxmox, request):
        captured.append(request)
        return "UPID:pve:config"

    monkeypatch.setattr(provision_stream_route, "_configure_cloud_init_vm", _configure)
    request = CloudVMProvisionRequest.model_validate(
        {
            **_valid_request_payload(),
            "extra_disks": [{"bus": "scsi", "index": 1, "size_gb": 100}],
            "tags": ["PBS"],
            "description": "PBS host",
        }
    )

    upid, error = await provision_stream_route._configure_cloud_init_for_stream(object(), request)

    assert (upid, error) == ("UPID:pve:config", None)
    assert captured == [request]


@pytest.mark.asyncio
async def test_enforce_cloud_network_overrides_caller_bridge_vlan_and_ip(monkeypatch):
    from proxbox_api.services.cloud_network import AllocatedIPAddress, CloudNetworkConfig

    async def _fake_get_netbox_async_session(*, database_session):
        assert database_session == "db-session"
        return "netbox-session"

    async def _fake_allocate_ip(prefix_id, *, netbox_session=None, **_kwargs):
        assert prefix_id == 123
        assert netbox_session == "netbox-session"
        return AllocatedIPAddress(id=77, address="168.0.98.10", cidr="168.0.98.10/24")

    monkeypatch.setattr(
        provision_route,
        "resolve_cloud_network",
        lambda: CloudNetworkConfig(
            lock_enabled=True,
            prefix_id=123,
            bridge="vmbr1",
            vlan_tag=2050,
            gateway="168.0.98.1",
        ),
    )
    monkeypatch.setattr(provision_route, "get_netbox_async_session", _fake_get_netbox_async_session)
    monkeypatch.setattr(provision_route, "allocate_ip", _fake_allocate_ip)

    request, lease = await provision_route._prepare_qemu_cloud_network_request(
        CloudVMProvisionRequest(
            endpoint_id=1,
            template_vmid=9000,
            new_vmid=9100,
            new_name="tenant-vm-9100",
            target_node="pve",
            cloud_init=CloudInitPayload(
                user="root",
                network={"ip": "dhcp", "gw": "10.0.0.1"},
            ),
            bridge="vmbr0",
            vlan_tag=7,
            enforce_cloud_network=True,
        ),
        "db-session",
    )

    assert request.bridge == "vmbr1"
    assert request.vlan_tag == 2050
    assert request.cloud_init.network == {"ip": "168.0.98.10/24", "gw": "168.0.98.1"}
    assert lease is not None
    assert lease.ip_id == 77


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
