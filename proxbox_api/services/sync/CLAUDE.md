# proxbox_api/services/sync Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/services/sync/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Synchronization services responsible for NetBox object creation from Proxmox data.

## Current Modules

- `__init__.py`: sync service namespace for Proxmox-to-NetBox flows.
- `clusters.py`: cluster synchronization helpers.
- `device_ensure.py`: device creation and reconciliation helpers.
- `devices.py`: device synchronization from Proxmox nodes to NetBox.
- `network.py`: network and interface sync helpers.
- `reconciliation/`: pure operation-queue reconciliation, Python fallback,
  optional Rust bridge, mismatch metric, and shared VM operation types.
- `snapshots.py`: snapshot sync helpers.
- `storage_links.py`: storage-to-NetBox relationship helpers.
- `storages.py`: storage sync helpers.
- `task_history.py`: NetBox task history and journal helpers.
- `virtual_disks.py`: VM disk sync helpers.
- `virtual_machines.py`: virtual machine payload and sync helpers.
- `vm_coordinator.py`: VM sync orchestration.
- `vm_create.py`: VM create path helpers.
- `vm_filter.py`: VM filtering helpers.
- `vm_helpers.py`: shared VM helper functions, including `record_id()` (extracts
  a NetBox record id from dict/serialized/object values) and
  `resolve_netbox_cluster_id_by_name()` (read-only cluster-id lookup by name,
  with optional caching; returns `None` when the cluster does not exist). These
  back the `(cluster_id, vmid)` scoping that keeps same-`vmid` VMs on different
  clusters from being conflated (issue #223).
- `vm_network.py`: VM network sync helpers.
- `vm_network_processor.py`: VM network parsing and processing helpers.
- `vmid_helpers.py`: VMID lookup and coordination helpers.
- `individual/`: targeted single-object sync workflows.

## How These Services Work

- Route handlers call these helpers to keep HTTP orchestration thin.
- These modules implement idempotent Proxmox-to-NetBox sync flows and journal tracking.
- The VM helpers split orchestration, filtering, network processing, and object creation so the route layer does not need to duplicate state handling.
- `reconciliation/` is the deterministic sync seam: it receives prepared state
  and NetBox snapshots, returns queue operations, and performs no I/O.
- `virtual_disks.py` resolves VM config targets from live Proxmox
  `cluster/resources` VMID/type data before falling back to NetBox VM custom
  fields or `device.name`; this avoids disk sync calls against stale or FQDN
  NetBox node names.
- **IP ownership invariant (all sync paths).** IP sync must never reassign an
  address that already belongs to a *different* object. The shared helper
  `ip_ownership.py` (`_reconcile_interface_ip`) resolves ownership before
  writing: it reuses an IP already on this interface, adopts an *unassigned*
  IP, or creates a new record scoped to this interface — a foreign-owned
  address is left untouched. It is parameterized by `assigned_object_type` /
  `interface_lookup_field` so it serves both `virtualization.vminterface`
  (VM interfaces) and `dcim.interface` (node interfaces). All write paths use
  this rule: `network.py::_resolve_vm_interface_ips` (per-VM-interface),
  `network.py::sync_node_interface_and_ip` (DCIM node IPs),
  `network.py::bulk_reconcile_vm_interface_ips` (bulk — scoped via
  `base_query` + `lookup_fields=["address", "assigned_object_id"]` so a
  foreign-owned address never suppresses creation), and
  `individual/ip_sync.py::sync_ip_individual`. `vm_network.py`
  (`ensure_ip_assigned_to_vm`) likewise only adopts unassigned
  IPs onto a VM and returns `assigned_to_other_object` instead of stealing an
  address owned elsewhere. This prevents the "VM interface wrongly matched to
  another server's IP" defect; both paths stay idempotent across re-syncs.

## Extension Guidance

- Keep sync routines idempotent where possible.
- Emit structured errors with `ProxboxException` for route-level handling.
- Keep progress reporting compatible with both WebSocket and SSE transport.
- Prefer small helper functions for object-specific concerns instead of growing a single coordinator module.
- For VM queue changes, update `tests/reconciliation/` and preserve
  Rust/Python parity before touching dispatch behavior.
