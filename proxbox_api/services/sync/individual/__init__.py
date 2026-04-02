"""Individual per-object sync services from Proxmox to NetBox.

This package provides completely independent per-object synchronization,
where each sync operation touches only one specific object using
targeted Proxmox API calls rather than bulk fetching.
"""

from proxbox_api.services.sync.individual.backup_sync import sync_backup_individual
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService
from proxbox_api.services.sync.individual.cluster_sync import sync_cluster_individual
from proxbox_api.services.sync.individual.device_sync import sync_node_individual
from proxbox_api.services.sync.individual.helpers import (
    build_disk_lookup_key,
    build_interface_lookup_key,
    normalize_mac,
    resolve_proxmox_session,
)
from proxbox_api.services.sync.individual.interface_sync import sync_interface_individual
from proxbox_api.services.sync.individual.ip_sync import sync_ip_individual
from proxbox_api.services.sync.individual.snapshot_sync import sync_snapshot_individual
from proxbox_api.services.sync.individual.storage_sync import sync_storage_individual
from proxbox_api.services.sync.individual.task_history_sync import (
    sync_task_history_individual,
)
from proxbox_api.services.sync.individual.virtual_disk_sync import (
    sync_virtual_disk_individual,
)
from proxbox_api.services.sync.individual.vm_sync import (
    sync_vm_individual,
    sync_vm_with_related,
)

__all__ = [
    "BaseIndividualSyncService",
    "normalize_mac",
    "resolve_proxmox_session",
    "build_interface_lookup_key",
    "build_disk_lookup_key",
    "sync_cluster_individual",
    "sync_node_individual",
    "sync_vm_individual",
    "sync_vm_with_related",
    "sync_interface_individual",
    "sync_ip_individual",
    "sync_task_history_individual",
    "sync_storage_individual",
    "sync_virtual_disk_individual",
    "sync_backup_individual",
    "sync_snapshot_individual",
]
