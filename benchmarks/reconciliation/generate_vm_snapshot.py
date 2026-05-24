"""Synthetic VM reconciliation dataset generator."""

from __future__ import annotations

from typing import Any

DEFAULT_FLAGS = {
    "overwrite_vm_role": True,
    "overwrite_vm_type": True,
    "overwrite_vm_tags": True,
    "overwrite_vm_description": True,
    "overwrite_vm_custom_fields": True,
    "supports_virtual_machine_type_field": True,
}


def build_vm_dataset(
    prepared_count: int,
    snapshot_count: int | None = None,
    *,
    pathological: bool = False,
) -> dict[str, Any]:
    """Build a deterministic VM reconciliation dataset."""

    if snapshot_count is None:
        snapshot_count = prepared_count

    prepared_vms: list[dict[str, Any]] = []
    snapshot: list[dict[str, Any]] = []

    for index in range(prepared_count):
        vmid = 1000 + index
        vm_type = "lxc" if index % 4 == 1 else "qemu"
        name = f"{vm_type}-{vmid}"
        desired_memory = 1024 + (index % 8) * 512
        desired_tags = [99, 200 + (index % 5)] if index % 6 == 0 else [99]

        prepared_vms.append(
            _prepared_vm_fixture(
                vmid=vmid,
                vm_type=vm_type,
                name=name,
                memory=desired_memory,
                tags=desired_tags,
                cluster={"id": 1} if index % 7 == 0 else 1,
            )
        )

        if _should_skip_snapshot(index, pathological):
            continue

        snapshot_record = _snapshot_vm_fixture(
            record_id=200000 + index,
            vmid=vmid,
            vm_type=vm_type,
            name=name,
            memory=desired_memory,
            tags=[{"id": 77}] if index % 6 == 0 else [{"id": 99}],
            cluster={"id": 1} if index % 3 else 1,
        )

        if index % 5 == 2:
            snapshot_record["memory"] = max(512, desired_memory - 512)
        if index % 5 == 3:
            snapshot_record["description"] = "Operator-maintained description"
        if pathological and index % 11 == 0:
            snapshot_record["custom_fields"] = {"proxmox_vm_id": "not-an-int"}
        if pathological and index % 13 == 0:
            snapshot_record.pop("cluster", None)
        snapshot.append(snapshot_record)

    while len(snapshot) < snapshot_count:
        filler_index = len(snapshot) + prepared_count
        snapshot.append(
            _snapshot_vm_fixture(
                record_id=300000 + filler_index,
                vmid=900000 + filler_index,
                vm_type="qemu",
                name=f"orphan-{filler_index}",
                memory=2048,
                tags=[{"id": 404}],
                cluster={"id": 1},
            )
        )

    return {
        "prepared_vms": prepared_vms,
        "netbox_snapshot": snapshot[:snapshot_count],
        "flags": dict(DEFAULT_FLAGS),
    }


def _should_skip_snapshot(index: int, pathological: bool) -> bool:
    if index % 5 == 1:
        return True
    return pathological and index % 4 == 0


def _prepared_vm_fixture(
    *,
    vmid: int,
    vm_type: str,
    name: str,
    memory: int,
    tags: list[int],
    cluster: object,
) -> dict[str, Any]:
    return {
        "cluster_name": "cluster-a",
        "resource": {"name": name, "vmid": vmid, "type": vm_type},
        "vm_config": {},
        "desired_payload": {
            "name": name,
            "status": "active",
            "cluster": cluster,
            "device": 10,
            "role": 20,
            "vcpus": 2,
            "memory": memory,
            "disk": 30,
            "tags": tags,
            "custom_fields": {"proxmox_vm_id": vmid, "proxmox_vm_type": vm_type},
            "description": "Synced from Proxmox node pve01",
        },
        "lookup": {"cf_proxmox_vm_id": vmid, "cluster_id": 1},
        "vm_type": vm_type,
    }


def _snapshot_vm_fixture(
    *,
    record_id: int,
    vmid: int,
    vm_type: str,
    name: str,
    memory: int | float,
    tags: list[object],
    cluster: object,
) -> dict[str, Any]:
    return {
        "id": record_id,
        "name": name,
        "status": "active",
        "cluster": cluster,
        "device": {"id": 10},
        "role": {"id": 20},
        "vcpus": 2.0 if vmid % 9 == 0 else 2,
        "memory": float(memory) if vmid % 10 == 0 else memory,
        "disk": 30,
        "tags": tags,
        "custom_fields": {"proxmox_vm_id": vmid, "proxmox_vm_type": vm_type},
        "description": "Synced from Proxmox node pve01",
    }
