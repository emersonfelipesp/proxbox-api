# proxbox_api/services/sync Directory Guide

## Purpose

Synchronization services responsible for NetBox object creation from Proxmox data.

## Current Modules

- `__init__.py`: Sync service namespace for Proxmox-to-NetBox flows.
- `clusters.py`: Cluster synchronization helpers.
- `device_ensure.py`: Device creation and reconciliation helpers.
- `devices.py`: Device synchronization from Proxmox nodes to NetBox.
- `network.py`: Network and interface sync helpers.
- `snapshots.py`: Snapshot sync helpers.
- `storage_links.py`: Storage-to-NetBox relationship helpers.
- `storages.py`: Storage sync helpers.
- `task_history.py`: NetBox task history and journal helpers.
- `virtual_disks.py`: VM disk sync helpers.
- `virtual_machines.py`: Virtual machine payload and sync helpers.
- `vm_coordinator.py`: VM sync orchestration.
- `vm_create.py`: VM create path helpers.
- `vm_filter.py`: VM filtering helpers.
- `vm_helpers.py`: Shared VM helper functions.
- `vm_network.py`: VM network sync helpers.
- `vm_network_processor.py`: VM network parsing and processing helpers.
- `vmid_helpers.py`: VMID lookup and coordination helpers.

## Key Data Flow and Dependencies

- These modules implement idempotent Proxmox-to-NetBox synchronization flows and journal tracking.
- Route handlers consume the helpers here to keep HTTP orchestration thin.

## Extension Guidance

- Keep sync routines idempotent where possible to support repeated runs.
- Emit structured errors with `ProxboxException` for route-level handling.
- When adding progress reporting, keep payload shapes compatible with both websocket and SSE transport.
