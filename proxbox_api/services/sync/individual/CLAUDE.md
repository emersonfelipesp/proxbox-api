# proxbox_api/services/sync/individual Directory Guide

## Purpose

Per-object synchronization services that sync individual objects from Proxmox to NetBox using targeted API calls with specific path and query parameters. This package provides standalone sync functionality independent from bulk sync mechanisms.

## Current Modules

- `__init__.py`: package exports for all individual sync functions.
- `base.py`: `BaseIndividualSyncService` with reusable helper methods for dependency creation.
- `helpers.py`: utility functions such as `normalize_mac()`, `resolve_proxmox_session()`, lookup-key builders, and Proxmox config parsers.
- `cluster_sync.py`: `sync_cluster_individual()` for a single cluster.
- `device_sync.py`: `sync_node_individual()` for a single node or device.
- `vm_sync.py`: `sync_vm_individual()` and `sync_vm_with_related()` for a single VM.
- `interface_sync.py`: `sync_interface_individual()` for a single interface.
- `ip_sync.py`: `sync_ip_individual()` for a single IP address.
- `task_history_sync.py`: `sync_task_history_individual()` for VM task history.
- `storage_sync.py`: `sync_storage_individual()` for a single storage.
- `virtual_disk_sync.py`: `sync_virtual_disk_individual()` for a single virtual disk.
- `backup_sync.py`: `sync_backup_individual()` for a single backup.
- `snapshot_sync.py`: `sync_snapshot_individual()` for a single snapshot.
- `replication_sync.py`: `sync_replication_individual()` for a single replication job.
- `backup_routine_sync.py`: `sync_backup_routine_individual()` for a single backup routine.

## Key Design Rules

1. Write-only operations are create or update only.
2. All sync functions support `dry_run`.
3. Dry runs should report `dependencies_synced`.
4. Missing dependencies should be created automatically instead of failing early.
5. Use targeted Proxmox endpoints rather than broad bulk fetches.

## Dependency Order

1. Cluster.
2. Nodes, which depend on cluster data.
3. Virtual machines, which depend on cluster and node data.
4. Interfaces and task history in parallel, which depend on VMs.
5. Storage, which depends on cluster data.
6. Virtual disks, backups, and snapshots in parallel, which depend on VMs and storage.

## Response Shape

All sync functions return a dict with these keys:

```python
{
    "object_type": str,
    "action": str,
    "proxmox_resource": dict,
    "netbox_object": dict | None,
    "dry_run": bool,
    "dependencies_synced": list,
    "error": str | None,
}
```

## Extension Guidance

- Follow the existing module pattern when adding new sync functions.
- Use `BaseIndividualSyncService` for common dependency creation logic.
- Use `rest_reconcile_async()` for find-or-create patterns with NetBox.
- Use `asyncio.gather(..., return_exceptions=True)` for parallel operations with shared dependencies.
- Keep functions focused on a single object type; use `sync_vm_with_related()` for coordinated VM workflows.
