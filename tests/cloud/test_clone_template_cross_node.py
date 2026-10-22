"""Cross-node cloud clone and disk-relocation behavior."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from proxbox_api.routes.cloud import provision as provision_route
from proxbox_api.routes.intent.cloud_init import CloudInitPayload
from proxbox_api.schemas.cloud_provision import CloudVMProvisionRequest, ExtraNetwork

_ALLOWED_RESOURCE_TYPES = {"vm", "storage", "node", "sdn"}


class _ProxmoxParameterError(Exception):
    """Stands in for Proxmox's HTTP 400 parameter verification failure."""


class _Endpoint:
    def __init__(self, session: _FakeProxmoxApi, path: tuple[str, ...]) -> None:
        self._session = session
        self._path = path

    def __getattr__(self, name: str) -> _Endpoint:
        return _Endpoint(self._session, (*self._path, name))

    def __call__(self, segment: object) -> _Endpoint:
        return _Endpoint(self._session, (*self._path, str(segment)))

    def get(self, **params: Any) -> object:
        return self._session.handle("GET", self._path, params)

    def post(self, **params: Any) -> object:
        return self._session.handle("POST", self._path, params)


_DEFAULT_TARGET_CONFIG = object()
_CLONED_VM_CONFIG: dict[str, Any] = {
    "scsi0": "shared-nfs:vm-10160-disk-0",
    "scsi1": "local-zfs-hdd:vm-10160-disk-1",
    "efidisk0": "shared-nfs:vm-10160-disk-2,efitype=4m",
    "tpmstate0": "local-zfs-hdd:vm-10160-disk-3,version=v2.0",
    "ide2": "shared-nfs:cloudinit",
    "sata0": "shared-nfs:iso/debian.iso,media=cdrom",
}


class _FakeProxmoxApi:
    """Two-node cluster with template 9243 owned by node01."""

    def __init__(
        self,
        *,
        shared: bool = False,
        storage_lookup_fails: bool = False,
        target_storage: bool = True,
        storage_status: str | None = None,
        empty_storage_inventory: bool = False,
        fail_move_disk: str | None = None,
        target_config: object = _DEFAULT_TARGET_CONFIG,
        target_config_fails: bool = False,
    ) -> None:
        self.calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []
        self.target_config = target_config
        self.target_config_fails = target_config_fails
        self.shared = shared
        self.storage_lookup_fails = storage_lookup_fails
        self.target_storage = target_storage
        self.storage_status = storage_status
        self.empty_storage_inventory = empty_storage_inventory
        self.fail_move_disk = fail_move_disk

    def __call__(self, path: str) -> _Endpoint:
        return _Endpoint(self, tuple(path.split("/")))

    def __getattr__(self, name: str) -> _Endpoint:
        return _Endpoint(self, (name,))

    def _cluster_resources(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        resource_type = params.get("type")
        if resource_type is not None and resource_type not in _ALLOWED_RESOURCE_TYPES:
            raise _ProxmoxParameterError(f"type: value {resource_type!r} is not allowed")
        if resource_type != "storage":
            return [
                {"id": "qemu/9243", "type": "qemu", "vmid": 9243, "node": "node01"},
                {"id": "lxc/9243", "type": "lxc", "vmid": 9243, "node": "node05"},
                {"id": "node/node04", "type": "node", "node": "node04"},
            ]
        if self.storage_lookup_fails:
            raise _ProxmoxParameterError("storage lookup failed")
        if self.empty_storage_inventory:
            return []
        rows = []
        if self.target_storage:
            target_row = {
                "id": "storage/node04/local-zfs-hdd",
                "type": "storage",
                "storage": "local-zfs-hdd",
                "node": "node04",
                "shared": 0,
            }
            if self.storage_status is not None:
                target_row["status"] = self.storage_status
            rows.append(target_row)
        if self.shared:
            rows.append(
                {
                    "id": "storage/node01/local-zfs-hdd",
                    "type": "storage",
                    "storage": "local-zfs-hdd",
                    "node": "node01",
                    "shared": 1,
                }
            )
        return rows

    def _qemu_config(self, path: tuple[str, ...]) -> object:
        if path[1:4] == ("node01", "qemu", "9243"):
            return {"name": "pbs-4-2-debian-13-cluster01", "template": 1}
        if path[1:4] == ("node04", "qemu", "10160"):
            if self.target_config_fails:
                raise TimeoutError("config read timed out")
            if self.target_config is _DEFAULT_TARGET_CONFIG:
                return dict(_CLONED_VM_CONFIG)
            return self.target_config
        raise _ProxmoxParameterError(f"configuration for {'/'.join(path)} does not exist")

    def handle(self, method: str, path: tuple[str, ...], params: dict[str, Any]) -> object:
        self.calls.append((method, path, params))
        if path == ("cluster", "resources"):
            return self._cluster_resources(params)
        if path == ("nodes", "node01", "qemu", "9243", "clone") and method == "POST":
            if params.get("target") != "node01" and params.get("storage") and not self.shared:
                raise _ProxmoxParameterError(
                    f"storage {params['storage']!r} is not available on node 'node01'"
                )
            return "UPID:node01:00000001:00000001:00000001:qmclone:9243:root@pam:"
        if path == ("nodes", "node04", "qemu", "10160", "move_disk") and method == "POST":
            if params["disk"] == self.fail_move_disk:
                raise _ProxmoxParameterError(f"move failed for {params['disk']}")
            return f"UPID:node04:move:{params['disk']}"
        if path[:1] == ("nodes",) and path[-1] == "config" and method == "GET":
            return self._qemu_config(path)
        raise AssertionError(f"unexpected Proxmox call: {method} {'/'.join(path)} {params}")


class _FakeProxmoxSession:
    def __init__(self, **kwargs: Any) -> None:
        self.session = _FakeProxmoxApi(**kwargs)


def _request(target_node: str) -> CloudVMProvisionRequest:
    return CloudVMProvisionRequest(
        endpoint_id=14,
        template_vmid=9243,
        new_vmid=10160,
        new_name="PBS06",
        target_node=target_node,
        storage="local-zfs-hdd",
        cloud_init=CloudInitPayload(user="root"),
    )


@pytest.fixture(autouse=True)
def _no_task_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision_route, "_should_wait_for_upid", lambda: False)


async def test_cross_node_non_shared_storage_clones_then_relocates_data_disks() -> None:
    proxmox = _FakeProxmoxSession()

    result = await provision_route._clone_template_vm(proxmox, _request("node04"))

    assert result.clone_upid is not None and result.clone_upid.startswith("UPID:node01:")
    assert result.relocate_upids == [
        "UPID:node04:move:scsi0",
        "UPID:node04:move:efidisk0",
    ]
    resource_queries = [
        params for method, path, params in proxmox.session.calls if path == ("cluster", "resources")
    ]
    assert resource_queries == [{"type": "vm"}, {"type": "storage"}]
    clones = [
        (path, params)
        for method, path, params in proxmox.session.calls
        if method == "POST" and path[-1] == "clone"
    ]
    assert clones == [
        (
            ("nodes", "node01", "qemu", "9243", "clone"),
            {
                "newid": 10160,
                "name": "PBS06",
                "target": "node04",
                "full": 1,
            },
        )
    ]
    moves = [
        (path, params)
        for method, path, params in proxmox.session.calls
        if method == "POST" and path[-1] == "move_disk"
    ]
    assert moves == [
        (
            ("nodes", "node04", "qemu", "10160", "move_disk"),
            {"disk": "scsi0", "storage": "local-zfs-hdd", "delete": 0},
        ),
        (
            ("nodes", "node04", "qemu", "10160", "move_disk"),
            {"disk": "efidisk0", "storage": "local-zfs-hdd", "delete": 0},
        ),
    ]


async def test_cross_node_shared_storage_uses_direct_clone_without_relocation() -> None:
    proxmox = _FakeProxmoxSession(shared=True)

    result = await provision_route._clone_template_vm(proxmox, _request("node04"))

    assert result.relocate_upids == []
    clone = next(call for call in proxmox.session.calls if call[1][-1] == "clone")
    assert clone[2]["storage"] == "local-zfs-hdd"
    assert not [call for call in proxmox.session.calls if call[1][-1] == "move_disk"]


async def test_same_node_clone_is_unchanged_without_storage_lookup() -> None:
    proxmox = _FakeProxmoxSession()

    result = await provision_route._clone_template_vm(proxmox, _request("node01"))

    assert result.relocate_upids == []
    resource_queries = [
        call[2] for call in proxmox.session.calls if call[1] == ("cluster", "resources")
    ]
    assert resource_queries == [{"type": "vm"}]
    clone = next(call for call in proxmox.session.calls if call[1][-1] == "clone")
    assert clone[2]["storage"] == "local-zfs-hdd"


async def test_cloud_init_cdrom_and_destination_disks_are_not_relocated() -> None:
    proxmox = _FakeProxmoxSession()

    await provision_route._clone_template_vm(proxmox, _request("node04"))

    moves = [call[2]["disk"] for call in proxmox.session.calls if call[1][-1] == "move_disk"]
    assert moves == ["scsi0", "efidisk0"]


async def test_storage_lookup_failure_fails_closed_before_clone() -> None:
    proxmox = _FakeProxmoxSession(storage_lookup_fails=True)

    with pytest.raises(HTTPException) as exc_info:
        await provision_route._clone_template_vm(proxmox, _request("node04"))

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == {
        "reason": "storage_lookup_failed",
        "storage": "local-zfs-hdd",
    }
    assert not [call for call in proxmox.session.calls if call[1][-1] == "clone"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_storage": False},
        {"empty_storage_inventory": True},
        {"storage_status": "unavailable"},
    ],
)
async def test_unavailable_target_storage_fails_before_clone(kwargs: dict[str, Any]) -> None:
    proxmox = _FakeProxmoxSession(**kwargs)

    with pytest.raises(HTTPException) as exc_info:
        await provision_route._clone_template_vm(proxmox, _request("node04"))

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == {
        "reason": "target_storage_unavailable",
        "storage": "local-zfs-hdd",
        "target_node": "node04",
    }
    assert not [call for call in proxmox.session.calls if call[1][-1] == "clone"]


async def test_clone_wait_finishes_before_target_config_and_disk_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxmox = _FakeProxmoxSession()

    async def _wait(_proxmox: object, node: str, upid: str) -> None:
        proxmox.session.calls.append(("WAIT", (node, upid), {}))

    monkeypatch.setattr(provision_route, "_should_wait_for_upid", lambda: True)
    monkeypatch.setattr(provision_route, "_wait_for_upid", _wait)

    await provision_route._clone_template_vm(proxmox, _request("node04"))

    clone_wait = next(i for i, call in enumerate(proxmox.session.calls) if call[0] == "WAIT")
    config_read = next(
        i
        for i, call in enumerate(proxmox.session.calls)
        if call[0] == "GET" and call[1] == ("nodes", "node04", "qemu", "10160", "config")
    )
    first_move = next(
        i for i, call in enumerate(proxmox.session.calls) if call[1][-1] == "move_disk"
    )
    assert clone_wait < config_read < first_move


async def test_relocation_failure_preserves_dispatched_upids_and_never_deletes() -> None:
    proxmox = _FakeProxmoxSession(fail_move_disk="efidisk0")
    request = _request("node04")
    plan = await provision_route._plan_template_clone(proxmox, request)
    clone_upid = await provision_route._dispatch_template_clone(proxmox, request, plan)

    with pytest.raises(HTTPException) as exc_info:
        await provision_route._relocate_vm_disks(proxmox, request, plan, clone_upid)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == {
        "reason": "relocate_disk_failed",
        "vmid": 10160,
        "target_node": "node04",
        "clone_upid": clone_upid,
        "relocate_upids": {"scsi0": "UPID:node04:move:scsi0"},
        "failed_disk": "efidisk0",
        "error": "move failed for efidisk0",
    }
    moves = [call for call in proxmox.session.calls if call[1][-1] == "move_disk"]
    assert moves and all(call[2]["delete"] == 0 for call in moves)
    assert not [
        call for call in proxmox.session.calls if "delete" in call[1] or "destroy" in call[1]
    ]


async def test_clone_reports_missing_template_without_cloning() -> None:
    proxmox = _FakeProxmoxSession()
    request = _request("node04").model_copy(update={"template_vmid": 9999})

    with pytest.raises(HTTPException) as exc_info:
        await provision_route._clone_template_vm(proxmox, request)

    assert exc_info.value.status_code == 404
    assert not [call for call in proxmox.session.calls if call[0] == "POST"]


@pytest.mark.parametrize(
    "target_config",
    [
        None,
        [],
        {"data": None},
        {"ide2": "shared-nfs:cloudinit", "name": "PBS06"},
    ],
    ids=["null", "list", "empty-data-envelope", "no-disk"],
)
async def test_degraded_relocation_config_fails_closed_with_clone_evidence(
    target_config: object,
) -> None:
    proxmox = _FakeProxmoxSession(target_config=target_config)
    request = _request("node04")
    plan = await provision_route._plan_template_clone(proxmox, request)
    clone_upid = await provision_route._dispatch_template_clone(proxmox, request, plan)

    with pytest.raises(HTTPException) as exc_info:
        await provision_route._relocate_vm_disks(proxmox, request, plan, clone_upid)

    assert exc_info.value.status_code == 502
    detail = exc_info.value.detail
    assert detail["reason"] == "relocate_disk_failed"
    assert detail["vmid"] == 10160
    assert detail["target_node"] == "node04"
    assert detail["clone_upid"] == clone_upid
    assert detail["relocate_upids"] == {}
    assert detail["failed_disk"] is None
    assert not [call for call in proxmox.session.calls if call[1][-1] == "move_disk"]


async def test_relocation_config_read_timeout_keeps_clone_evidence() -> None:
    proxmox = _FakeProxmoxSession(target_config_fails=True)
    request = _request("node04")
    plan = await provision_route._plan_template_clone(proxmox, request)
    clone_upid = await provision_route._dispatch_template_clone(proxmox, request, plan)

    with pytest.raises(HTTPException) as exc_info:
        await provision_route._relocate_vm_disks(proxmox, request, plan, clone_upid)

    detail = exc_info.value.detail
    assert exc_info.value.status_code == 502
    assert detail["reason"] == "relocate_disk_failed"
    assert detail["clone_upid"] == clone_upid
    assert detail["failed_disk"] is None
    assert detail["error"] == "config read timed out"
    assert not [call for call in proxmox.session.calls if call[1][-1] == "move_disk"]


def test_extra_networks_refuse_unreadable_config() -> None:
    request = _request("node04").model_copy(
        update={"extra_networks": [ExtraNetwork(index=1, bridge="vmbr1", vlan_tag=20)]}
    )

    with pytest.raises(HTTPException) as exc_info:
        provision_route._build_extra_network_args(request, None)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == {"reason": "config_read_failed"}
    assert provision_route._build_extra_network_args(request, {"scsi0": "x:vm-1-disk-0"}) == {
        "net1": "virtio,bridge=vmbr1,tag=20"
    }
