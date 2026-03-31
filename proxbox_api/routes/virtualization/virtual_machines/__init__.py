"""Virtual machine routes: aggregate sub-routers for sync, reads, disks, backups, snapshots."""

from __future__ import annotations

from fastapi import APIRouter

from proxbox_api.routes.virtualization.virtual_machines import (
    backups_vm,
    disks_vm,
    read_vm,
    snapshots_vm,
    sync_vm,
)
from proxbox_api.routes.virtualization.virtual_machines.backups_vm import (
    _volids_from_proxmox_storage_backup_items,
    create_netbox_backups,
    process_backups_batch,
)
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    create_virtual_machines,
)

router = APIRouter()
router.include_router(sync_vm.router)
router.include_router(read_vm.router)
router.include_router(disks_vm.router)
router.include_router(backups_vm.router)
router.include_router(snapshots_vm.router)

__all__ = (
    "create_netbox_backups",
    "create_virtual_machines",
    "process_backups_batch",
    "router",
    "_volids_from_proxmox_storage_backup_items",
)
