# proxbox_api/services/sync/individual Directory Guide

## Purpose

Per-object synchronization services that sync individual objects from Proxmox to NetBox using targeted API calls with specific path/query parameters. This package provides standalone sync functionality independent from bulk sync mechanisms.

## Current Modules

- `__init__.py`: Package exports for all individual sync functions.
- `base.py`: `BaseIndividualSyncService` class with reusable helper methods for dependency creation (cluster, manufacturer, device type, device role, site, device, VM).
- `helpers.py`: Utility functions including `normalize_mac()`, `resolve_proxmox_session()`, `build_interface_lookup_key()`, `build_disk_lookup_key()`, `build_ip_lookup_key()`, `parse_key_value_string()`, `parse_disk_config_entry()`, `storage_name_from_volume_id()`.
- `cluster_sync.py`: `sync_cluster_individual()` - sync a single cluster.
- `device_sync.py`: `sync_node_individual()` - sync a single node/device (depends on Cluster).
- `vm_sync.py`: `sync_vm_individual()` and `sync_vm_with_related()` - sync a single VM (depends on Cluster, auto-creates all VM prerequisites).
- `interface_sync.py`: `sync_interface_individual()` - sync a single interface (depends on VM, auto-creates VM).
- `ip_sync.py`: `sync_ip_individual()` - sync a single IP address (depends on Interface/VM).
- `task_history_sync.py`: `sync_task_history_individual()` - sync task history for a VM.
- `storage_sync.py`: `sync_storage_individual()` - sync a single storage (depends on Cluster).
- `virtual_disk_sync.py`: `sync_virtual_disk_individual()` - sync a single virtual disk (depends on VM, Storage).
- `backup_sync.py`: `sync_backup_individual()` - sync a single backup (depends on VM, Storage).
- `snapshot_sync.py`: `sync_snapshot_individual()` - sync a single snapshot (depends on VM).

## Key Design Principles

1. **Write-only**: Operations are create/update only (no delete).
2. **Dry-run support**: All sync functions accept a `dry_run` parameter that returns what would be synced without making changes.
3. **Dependency display**: Dry-run responses include `dependencies_synced` list showing what would be created/updated.
4. **Auto-create dependencies**: If a dependency doesn't exist, it is auto-created rather than returning an error.
5. **Targeted API calls**: Uses specific Proxmox endpoints (e.g., `/nodes/{node}/{type}/{vmid}/config`) rather than bulk fetching.

## Dependency Order

The sync dependency order is:
1. Cluster
2. Nodes (depend on Cluster)
3. Virtual Machines (depend on Cluster and Node)
4. Interfaces and Task History (in parallel, depend on VM)
5. Storage (depends on Cluster)
6. Virtual Disks, Backups, and Snapshots (in parallel, depend on VM and Storage)

## Response Format

All sync functions return a dict with this structure:
```python
{
    "object_type": str,           # e.g., "vm", "interface", "cluster"
    "action": str,                # "created", "updated", "dry_run", "error"
    "proxmox_resource": dict,     # The Proxmox data that was/would be synced
    "netbox_object": dict|None,   # The NetBox object after sync (or None in dry-run)
    "dry_run": bool,              # True if this was a dry-run
    "dependencies_synced": list,  # List of dependencies that were synced
    "error": str|None,            # Error message if action was "error"
}
```

## Extension Guidance

- When adding new sync functions, follow the pattern of existing functions in this package.
- Use `BaseIndividualSyncService` for common dependency creation logic.
- Use `rest_reconcile_async()` for find-or-create patterns with NetBox.
- Use `asyncio.gather(*tasks, return_exceptions=True)` for parallel operations with common dependencies.
- Keep functions focused on single-object sync; use `sync_vm_with_related()` for coordinating multiple related syncs.
