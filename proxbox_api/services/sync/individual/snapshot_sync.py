"""Individual Snapshot sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxSnapshotSyncState
from proxbox_api.services.proxmox_helpers import get_vm_snapshots_individual


async def sync_snapshot_individual(
    nb: object,
    px: object,
    tag: object,
    node: str,
    vm_type: str,
    vmid: int,
    snapshot_name: str,
    auto_create_vm: bool = True,
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
        snapshots = get_vm_snapshots_individual(px, node, vm_type, vmid)
    except Exception:
        snapshots = []

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

                cluster_name = getattr(px, "name", "unknown")
                await sync_vm_individual(
                    nb, px, tag, cluster_name, node, vm_type, vmid, dry_run=False
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

        snapshot_payload: dict[str, object] = {
            "virtual_machine": vm_id,
            "name": snapshot_name,
            "vmid": vmid,
            "node": node,
            "description": target_snapshot.get("description") if target_snapshot else None,
            "snaptime": str(target_snapshot.get("snaptime", "")) if target_snapshot else None,
            "parent": target_snapshot.get("parent") if target_snapshot else None,
            "subtype": vm_type,
            "status": "active",
        }

        snapshot_record = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/snapshots/",
            lookup={"vmid": vmid, "name": snapshot_name},
            payload=snapshot_payload,
            schema=NetBoxSnapshotSyncState,
            current_normalizer=lambda record: {
                "virtual_machine": record.get("virtual_machine"),
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
        action = "created" if getattr(snapshot_record, "id", None) else "updated"

        return {
            "object_type": "snapshot",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": [{"object_type": "vm", "vmid": vmid, "action": action}],
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
