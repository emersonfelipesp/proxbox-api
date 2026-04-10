"""Individual Snapshot sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxSnapshotSyncState
from proxbox_api.services.proxmox_helpers import (
    get_vm_config_individual,
    get_vm_snapshots_individual,
)
from proxbox_api.services.sync.individual.helpers import (
    parse_disk_config_entry,
    storage_name_from_volume_id,
)


async def sync_snapshot_individual(  # noqa: C901
    nb: object,
    px: object,
    tag: object,
    node: str,
    vm_type: str,
    vmid: int,
    snapshot_name: str,
    cluster_name: str | None = None,
    auto_create_vm: bool = True,
    auto_create_storage: bool = True,
    dry_run: bool = False,
) -> dict:
    """Sync a single Snapshot from Proxmox to NetBox.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        node: Proxmox node name.
        vm_type: 'qemu' or 'lxc'.
        vmid: Proxmox VM ID.
        snapshot_name: Name of the snapshot to sync.
        auto_create_vm: Whether to auto-create the VM if it doesn't exist.
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict.
    """
    now = datetime.now(timezone.utc)

    try:
        snapshots = await get_vm_snapshots_individual(px, node, vm_type, vmid)
    except Exception:
        snapshots = []
    try:
        vm_config = await get_vm_config_individual(px, node, vm_type, vmid)
    except Exception:
        vm_config = {}

    target_snapshot = None
    for snap in snapshots:
        if str(snap.get("name", "")) == snapshot_name:
            target_snapshot = snap
            break

    proxmox_resource: dict[str, object] = {
        "vmid": vmid,
        "node": node,
        "type": vm_type,
        "snapshot_name": snapshot_name,
        "snapshot_data": target_snapshot,
        "cluster_name": cluster_name or getattr(px, "name", None),
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        existing_vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid},
        )
        netbox_object = None
        if existing_vms:
            vm_id = getattr(existing_vms[0], "id", None)
            if vm_id:
                existing = await rest_list_async(
                    nb,
                    "/api/plugins/proxbox/snapshots/",
                    query={"vmid": vmid, "name": snapshot_name},
                )
                if existing:
                    netbox_object = (
                        existing[0].serialize() if hasattr(existing[0], "serialize") else None
                    )

        vm_dep: dict[str, object] = {"object_type": "vm", "vmid": vmid}
        return {
            "object_type": "snapshot",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": [vm_dep],
            "error": None,
        }

    try:
        existing_vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid},
        )
        if not existing_vms:
            if auto_create_vm:
                from proxbox_api.services.sync.individual.vm_sync import sync_vm_individual

                await sync_vm_individual(
                    nb,
                    px,
                    tag,
                    cluster_name or getattr(px, "name", "unknown"),
                    node,
                    vm_type,
                    vmid,
                    dry_run=False,
                )
                existing_vms = await rest_list_async(
                    nb,
                    "/api/virtualization/virtual-machines/",
                    query={"cf_proxmox_vm_id": vmid},
                )
            else:
                return {
                    "object_type": "snapshot",
                    "action": "error",
                    "proxmox_resource": proxmox_resource,
                    "netbox_object": None,
                    "dry_run": False,
                    "dependencies_synced": [],
                    "error": f"VM with vmid={vmid} not found in NetBox",
                }

        vm_record = existing_vms[0]
        vm_id = getattr(vm_record, "id", None)
        if vm_id is None:
            return {
                "object_type": "snapshot",
                "action": "error",
                "proxmox_resource": proxmox_resource,
                "netbox_object": None,
                "dry_run": False,
                "dependencies_synced": [],
                "error": f"Could not resolve VM ID for vmid={vmid}",
            }

        storage_name = None
        for disk_key in ("rootfs", "scsi0", "virtio0", "ide0", "sata0"):
            disk_config = parse_disk_config_entry(vm_config.get(disk_key))
            volume_id = disk_config.get("volume", disk_config.get("file"))
            storage_name = storage_name_from_volume_id(volume_id)
            if storage_name:
                break

        storage_id: int | None = None
        if storage_name and auto_create_storage:
            from proxbox_api.services.sync.individual.storage_sync import sync_storage_individual

            storage_result = await sync_storage_individual(
                nb,
                px,
                tag,
                cluster_name or getattr(px, "name", "unknown"),
                storage_name,
                dry_run=False,
            )
            if storage_result.get("netbox_object"):
                storage_id = storage_result["netbox_object"].get("id")
        elif storage_name:
            existing_storage = await rest_list_async(
                nb,
                "/api/plugins/proxbox/storage/",
                query={"name": storage_name},
            )
            if existing_storage:
                storage_id = getattr(existing_storage[0], "id", None)

        snapshot_payload: dict[str, object] = {
            "virtual_machine": vm_id,
            "proxmox_storage": storage_id,
            "name": snapshot_name,
            "vmid": vmid,
            "node": node,
            "description": target_snapshot.get("description") if target_snapshot else None,
            "snaptime": (
                datetime.fromtimestamp(target_snapshot["snaptime"]).isoformat()
                if target_snapshot and target_snapshot.get("snaptime")
                else None
            ),
            "parent": target_snapshot.get("parent") if target_snapshot else None,
            "subtype": vm_type,
            "status": "active",
        }
        existing_snapshots = await rest_list_async(
            nb,
            "/api/plugins/proxbox/snapshots/",
            query={"vmid": vmid, "name": snapshot_name, "node": node},
        )

        snapshot_record = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/snapshots/",
            lookup={"vmid": vmid, "name": snapshot_name, "node": node},
            payload=snapshot_payload,
            schema=NetBoxSnapshotSyncState,
            current_normalizer=lambda record: {
                "virtual_machine": record.get("virtual_machine"),
                "proxmox_storage": record.get("proxmox_storage"),
                "name": record.get("name"),
                "vmid": record.get("vmid"),
                "node": record.get("node"),
                "description": record.get("description"),
                "snaptime": record.get("snaptime"),
                "parent": record.get("parent"),
                "subtype": record.get("subtype"),
                "status": record.get("status"),
            },
        )

        netbox_object = (
            snapshot_record.serialize() if hasattr(snapshot_record, "serialize") else None
        )
        action = "updated" if existing_snapshots else "created"

        dependencies = [{"object_type": "vm", "vmid": vmid, "action": action}]
        if storage_name:
            dependencies.append({"object_type": "storage", "name": storage_name, "action": action})
        return {
            "object_type": "snapshot",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": dependencies,
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "snapshot",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }
