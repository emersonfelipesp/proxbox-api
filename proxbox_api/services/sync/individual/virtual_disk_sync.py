"""Individual Virtual Disk sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualDiskSyncState
from proxbox_api.services.proxmox_helpers import get_vm_config_individual
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService
from proxbox_api.services.sync.individual.helpers import (
    build_disk_lookup_key,
    parse_disk_config_entry,
    storage_name_from_volume_id,
)


async def sync_virtual_disk_individual(
    nb: object,
    px: object,
    tag: object,
    node: str,
    vm_type: str,
    vmid: int,
    disk_name: str,
    auto_create_vm: bool = True,
    auto_create_storage: bool = True,
    dry_run: bool = False,
) -> dict:
    """Sync a single Virtual Disk from Proxmox to NetBox.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        node: Proxmox node name.
        vm_type: 'qemu' or 'lxc'.
        vmid: Proxmox VM ID.
        disk_name: Name of the disk (e.g., 'scsi0', 'virtio0', 'rootfs').
        auto_create_vm: Whether to auto-create the VM if it doesn't exist.
        auto_create_storage: Whether to auto-create the storage if it doesn't exist.
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict.
    """
    service = BaseIndividualSyncService(nb, px, tag)
    tag_refs = service.tag_refs
    now = datetime.now(timezone.utc)

    try:
        vm_config = get_vm_config_individual(px, node, vm_type, vmid)
    except Exception:
        vm_config = {}

    disk_config = parse_disk_config_entry(vm_config.get(disk_name))
    size = disk_config.get("size", "0")
    try:
        size_mb = int(size) // 1_000_000 if size else 0
    except (ValueError, TypeError):
        size_mb = 0

    volume_id = disk_config.get("volume", disk_config.get("file", None))
    storage_name = storage_name_from_volume_id(volume_id)

    proxmox_resource: dict[str, object] = {
        "vmid": vmid,
        "node": node,
        "type": vm_type,
        "disk_name": disk_name,
        "size": size_mb,
        "volume_id": volume_id,
        "storage": storage_name,
        "config": disk_config,
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        existing_vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid},
        )
        vm_id = None
        if existing_vms:
            vm_id = getattr(existing_vms[0], "id", None)

        netbox_object = None
        if vm_id:
            existing = await rest_list_async(
                nb,
                "/api/virtualization/virtual-disks/",
                query={"virtual_machine_id": vm_id, "name": disk_name},
            )
            if existing:
                netbox_object = (
                    existing[0].serialize() if hasattr(existing[0], "serialize") else None
                )

        vm_dep: dict[str, object] = {"object_type": "vm", "vmid": vmid}
        storage_dep: dict[str, object] | None = (
            {"object_type": "storage", "name": storage_name} if storage_name else None
        )
        deps = [vm_dep]
        if storage_dep:
            deps.append(storage_dep)

        return {
            "object_type": "virtual_disk",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": deps,
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
                    "object_type": "virtual_disk",
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
                "object_type": "virtual_disk",
                "action": "error",
                "proxmox_resource": proxmox_resource,
                "netbox_object": None,
                "dry_run": False,
                "dependencies_synced": [],
                "error": f"Could not resolve VM ID for vmid={vmid}",
            }

        storage_id: int | None = None
        if storage_name and auto_create_storage:
            from proxbox_api.services.sync.individual.storage_sync import sync_storage_individual

            cluster_name = getattr(px, "name", "unknown")
            storage_result = await sync_storage_individual(
                nb, px, tag, cluster_name, storage_name, dry_run=False
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

        disk_payload: dict[str, object] = {
            "virtual_machine": vm_id,
            "name": disk_name,
            "size": size_mb,
            "storage": storage_id,
            "description": f"Proxmox disk {disk_name} for VM {vmid}",
            "tags": tag_refs,
            "custom_fields": {"proxmox_last_updated": now.isoformat()},
        }

        disk_record = await rest_reconcile_async(
            nb,
            "/api/virtualization/virtual-disks/",
            lookup=build_disk_lookup_key(disk_name, vm_id),
            payload=disk_payload,
            schema=NetBoxVirtualDiskSyncState,
            current_normalizer=lambda record: {
                "virtual_machine": record.get("virtual_machine"),
                "name": record.get("name"),
                "size": record.get("size"),
                "storage": record.get("storage"),
                "description": record.get("description"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
            },
        )

        netbox_object = disk_record.serialize() if hasattr(disk_record, "serialize") else None
        action = "created" if getattr(disk_record, "id", None) else "updated"

        dependencies: list[dict] = [{"object_type": "vm", "vmid": vmid, "action": action}]
        if storage_name:
            dependencies.append({"object_type": "storage", "name": storage_name, "action": action})

        return {
            "object_type": "virtual_disk",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": dependencies,
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "virtual_disk",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }
