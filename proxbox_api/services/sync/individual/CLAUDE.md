# proxbox_api/services/sync/individual Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/services/sync/individual/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Per-object synchronization services that sync individual objects from Proxmox to NetBox using targeted API calls with specific path and query parameters. This package provides standalone sync functionality independent from bulk sync mechanisms.

## Current Modules

- `__init__.py`: package exports for all individual sync functions.
- `base.py`: `BaseIndividualSyncService` with reusable helper methods for dependency creation.
- `helpers.py`: utility functions such as `normalize_mac()`, `resolve_proxmox_session()`, lookup-key builders, Proxmox config parsers, and `ensure_vm_record()`.
- `cluster_sync.py`: `sync_cluster_individual()` for a single cluster.
- `device_sync.py`: `sync_node_individual()` for a single node or device.
- `vm_sync.py`: `sync_vm_individual()` and `sync_vm_with_related()` for a single VM.
- `interface_sync.py`: `sync_interface_individual()` for a single interface.
- `ip_sync.py`: `sync_ip_individual()` for a single IP address.
- `task_history_sync.py`: `sync_task_history_individual()` for VM task history.
  PVE archive rows identify the guest through `id` (with `vmid` compatibility),
  and nonnumeric non-VM IDs must be skipped safely. Archive `endtime` and final
  `exitstatus`/`status` are authoritative; persist the same final value to
  `status` and `exitstatus` with `task_state="stopped"`.
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

## Cross-Cluster VMID Scoping (issue #223)

`ensure_vm_record()` resolves the NetBox VM for a Proxmox `vmid` scoped by its
NetBox cluster, because `vmid` is only unique within a single Proxmox cluster.
Callers pass `cluster_name` (and optionally a pre-resolved `cluster_id`); the
helper resolves the cluster id by name via
`resolve_netbox_cluster_id_by_name` (`services/sync/vm_helpers.py`) and queries
`virtual-machines` with both `cf_proxmox_vm_id` **and** `cluster_id`. If the
cluster cannot be resolved, it falls back to a vmid-only match **only when it is
globally unambiguous** (exactly one NetBox VM); an ambiguous vmid is reported as
not-found instead of being mapped to the wrong cluster's VM. `snapshot_sync`,
`backup_sync`, and `task_history_sync` route their VM resolution through this
helper so every individual sync path is cluster-safe.

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
