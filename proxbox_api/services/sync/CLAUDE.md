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
- `snapshots.py`: snapshot sync helpers.
- `storage_links.py`: storage-to-NetBox relationship helpers.
- `storages.py`: storage sync helpers.
- `task_history.py`: NetBox task history and journal helpers.
- `virtual_disks.py`: VM disk sync helpers.
- `virtual_machines.py`: virtual machine payload and sync helpers.
- `vm_coordinator.py`: VM sync orchestration.
- `vm_create.py`: VM create path helpers.
- `vm_filter.py`: VM filtering helpers.
- `vm_helpers.py`: shared VM helper functions.
- `vm_network.py`: VM network sync helpers.
- `vm_network_processor.py`: VM network parsing and processing helpers.
- `vmid_helpers.py`: VMID lookup and coordination helpers.
- `individual/`: targeted single-object sync workflows.

## How These Services Work

- Route handlers call these helpers to keep HTTP orchestration thin.
- These modules implement idempotent Proxmox-to-NetBox sync flows and journal tracking.
- The VM helpers split orchestration, filtering, network processing, and object creation so the route layer does not need to duplicate state handling.

## Extension Guidance

- Keep sync routines idempotent where possible.
- Emit structured errors with `ProxboxException` for route-level handling.
- Keep progress reporting compatible with both WebSocket and SSE transport.
- Prefer small helper functions for object-specific concerns instead of growing a single coordinator module.
