"""Individual Backup sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxBackupSyncState
from proxbox_api.services.proxmox_helpers import get_vm_backups_individual
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService
from proxbox_api.services.sync.individual.helpers import (
    build_sync_response,
    ensure_vm_record,
    get_first_record,
    get_serialized_first_record,
)


async def _resolve_storage_id(
    nb: object,
    px: object,
    tag: object,
    storage: str,
    *,
    auto_create_storage: bool,
) -> int | None:
    if auto_create_storage:
        from proxbox_api.services.sync.individual.storage_sync import sync_storage_individual

        cluster_name = getattr(px, "name", "unknown")
        storage_result = await sync_storage_individual(
            nb, px, tag, cluster_name, storage, dry_run=False
        )
        netbox_object = storage_result.get("netbox_object")
        if isinstance(netbox_object, dict):
            return netbox_object.get("id")
        return None

    existing_storage = await rest_list_async(
        nb,
        "/api/plugins/proxbox/storage/",
        query={"name": storage},
    )
    if existing_storage:
        return getattr(existing_storage[0], "id", None)
    return None


async def sync_backup_individual(
    nb: object,
    px: object,
    tag: object,
    node: str,
    storage: str,
    vmid: int,
    volid: str,
    auto_create_vm: bool = True,
    auto_create_storage: bool = True,
    dry_run: bool = False,
) -> dict:
    """Sync a single Backup from Proxmox to NetBox.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        node: Proxmox node name.
        storage: Proxmox storage name.
        vmid: Proxmox VM ID.
        volid: Backup volume ID (e.g., 'local:backup/vm/100.dump').
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
        backups = get_vm_backups_individual(px, node, storage, vmid)
    except Exception:
        backups = []

    target_backup = None
    for backup in backups:
        if str(backup.get("volid", "")) == volid:
            target_backup = backup
            break

    proxmox_resource: dict[str, object] = {
        "vmid": vmid,
        "node": node,
        "storage": storage,
        "volid": volid,
        "backup_data": target_backup,
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        netbox_object = None
        vm_record = await get_first_record(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid},
        )
        if vm_record is not None:
            netbox_object = await get_serialized_first_record(
                nb,
                "/api/plugins/proxbox/backups/",
                query={"vmid": str(vmid), "volume_id": volid},
            )

        return build_sync_response(
            object_type="backup",
            action="dry_run",
            proxmox_resource=proxmox_resource,
            netbox_object=netbox_object,
            dry_run=True,
            dependencies_synced=[
                {"object_type": "vm", "vmid": vmid},
                {"object_type": "storage", "name": storage},
            ],
            error=None,
        )

    try:
        vm_record, vm_error = await ensure_vm_record(
            nb,
            px,
            tag,
            vmid=vmid,
            node=node,
            vm_type="qemu",
            auto_create_vm=auto_create_vm,
        )
        if vm_error:
            return build_sync_response(
                object_type="backup",
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
                object_type="backup",
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
            storage,
            auto_create_storage=auto_create_storage,
        )

        backup_payload: dict[str, object] = {
            "virtual_machine": vm_id,
            "proxmox_storage": storage_id,
            "subtype": target_backup.get("format") if target_backup else None,
            "size": target_backup.get("size") if target_backup else None,
            "volume_id": volid,
            "vmid": str(vmid),
            "notes": target_backup.get("notes") if target_backup else None,
            "tags": tag_refs,
        }

        existing_backups = await rest_list_async(
            nb,
            "/api/plugins/proxbox/backups/",
            query={"vmid": str(vmid), "volume_id": volid},
        )
        backup_record = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/backups/",
            lookup={"vmid": str(vmid), "volume_id": volid},
            payload=backup_payload,
            schema=NetBoxBackupSyncState,
            current_normalizer=lambda record: {
                "virtual_machine": record.get("virtual_machine"),
                "proxmox_storage": record.get("proxmox_storage"),
                "subtype": record.get("subtype"),
                "size": record.get("size"),
                "volume_id": record.get("volume_id"),
                "vmid": record.get("vmid"),
                "notes": record.get("notes"),
                "tags": record.get("tags"),
            },
        )

        netbox_object = backup_record.serialize() if hasattr(backup_record, "serialize") else None
        action = "updated" if existing_backups else "created"

        return build_sync_response(
            object_type="backup",
            action=action,
            proxmox_resource=proxmox_resource,
            netbox_object=netbox_object,
            dry_run=False,
            dependencies_synced=[
                {"object_type": "vm", "vmid": vmid, "action": action},
                {"object_type": "storage", "name": storage, "action": action},
            ],
            error=None,
        )

    except Exception as error:
        return build_sync_response(
            object_type="backup",
            action="error",
            proxmox_resource=proxmox_resource,
            netbox_object=None,
            dry_run=False,
            dependencies_synced=[],
            error=str(error),
        )
