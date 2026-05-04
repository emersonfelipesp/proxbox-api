"""Individual Virtual Disk sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualDiskSyncState
from proxbox_api.proxmox_to_netbox.schemas.disks import size_str_to_mb
from proxbox_api.services.proxmox_helpers import get_vm_config_individual
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService
from proxbox_api.services.sync.individual.helpers import (
    build_disk_lookup_key,
    build_sync_response,
    ensure_vm_record,
    get_serialized_first_record,
    parse_disk_config_entry,
    storage_name_from_volume_id,
)


async def _resolve_storage_id(
    nb: object,
    px: object,
    tag: object,
    *,
    storage_name: str | None,
    auto_create_storage: bool,
) -> int | None:
    if not storage_name:
        return None

    if auto_create_storage:
        from proxbox_api.services.sync.individual.storage_sync import sync_storage_individual

        cluster_name = getattr(px, "name", "unknown")
        storage_result = await sync_storage_individual(
            nb, px, tag, cluster_name, storage_name, dry_run=False
        )
        netbox_object = storage_result.get("netbox_object")
        if isinstance(netbox_object, dict):
            return netbox_object.get("id")
        return None

    existing_storage = await rest_list_async(
        nb,
        "/api/plugins/proxbox/storage/",
        query={"name": storage_name},
    )
    if existing_storage:
        return getattr(existing_storage[0], "id", None)
    return None


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
        vm_config = await get_vm_config_individual(px, node, vm_type, vmid)
    except Exception:
        vm_config = {}

    disk_config = parse_disk_config_entry(vm_config.get(disk_name))
    size = disk_config.get("size", "0")
    # Proxmox emits sizes as suffixed strings ("32G", "512M", "1T"). The previous
    # implementation did ``int(size) // 1_048_576`` which raises on any suffix
    # and silently fell back to 0 — making the individual sync path produce
    # virtual-disks with size 0 that then mismatched the VM-level disk total
    # under NetBox 4.5+ aggregate validation.
    size_mb = size_str_to_mb(size) if size else 0

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
        vm_record, _ = await ensure_vm_record(
            nb,
            px,
            tag,
            vmid=vmid,
            node=node,
            vm_type=vm_type,
            auto_create_vm=False,
        )
        vm_id = getattr(vm_record, "id", None) if vm_record is not None else None
        netbox_object = None
        if vm_id:
            netbox_object = await get_serialized_first_record(
                nb,
                "/api/virtualization/virtual-disks/",
                query={"virtual_machine_id": vm_id, "name": disk_name},
            )

        dependencies = [{"object_type": "vm", "vmid": vmid}]
        if storage_name:
            dependencies.append({"object_type": "storage", "name": storage_name})

        return build_sync_response(
            object_type="virtual_disk",
            action="dry_run",
            proxmox_resource=proxmox_resource,
            netbox_object=netbox_object,
            dry_run=True,
            dependencies_synced=dependencies,
            error=None,
        )

    try:
        vm_record, vm_error = await ensure_vm_record(
            nb,
            px,
            tag,
            vmid=vmid,
            node=node,
            vm_type=vm_type,
            auto_create_vm=auto_create_vm,
        )
        if vm_error:
            return build_sync_response(
                object_type="virtual_disk",
                action="error",
                proxmox_resource=proxmox_resource,
                netbox_object=None,
                dry_run=False,
                dependencies_synced=[],
                error=vm_error,
            )

        vm_id = getattr(vm_record, "id", None)
        if vm_id is None:
            return build_sync_response(
                object_type="virtual_disk",
                action="error",
                proxmox_resource=proxmox_resource,
                netbox_object=None,
                dry_run=False,
                dependencies_synced=[],
                error=f"Could not resolve VM ID for vmid={vmid}",
            )

        storage_id = await _resolve_storage_id(
            nb,
            px,
            tag,
            storage_name=storage_name,
            auto_create_storage=auto_create_storage,
        )

        disk_payload: dict[str, object] = {
            "virtual_machine": vm_id,
            "name": disk_name,
            "size": size_mb,
            "storage": storage_id,
            "description": f"Proxmox disk {disk_name} for VM {vmid}",
            "tags": tag_refs,
            "custom_fields": {"proxmox_last_updated": now.isoformat()},
        }

        existing_disks = await rest_list_async(
            nb,
            "/api/virtualization/virtual-disks/",
            query=build_disk_lookup_key(disk_name, vm_id),
        )
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
        action = "updated" if existing_disks else "created"

        dependencies: list[dict] = [{"object_type": "vm", "vmid": vmid, "action": action}]
        if storage_name:
            dependencies.append({"object_type": "storage", "name": storage_name, "action": action})

        return build_sync_response(
            object_type="virtual_disk",
            action=action,
            proxmox_resource=proxmox_resource,
            netbox_object=netbox_object,
            dry_run=False,
            dependencies_synced=dependencies,
            error=None,
        )

    except Exception as error:
        return build_sync_response(
            object_type="virtual_disk",
            action="error",
            proxmox_resource=proxmox_resource,
            netbox_object=None,
            dry_run=False,
            dependencies_synced=[],
            error=str(error),
        )
