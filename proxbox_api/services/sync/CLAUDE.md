# proxbox_api/services/sync Directory Guide

## Purpose

Synchronization services responsible for NetBox object creation from Proxmox data.

## Modules and Responsibilities

- `__init__.py`: Synchronization service namespace for Proxmox to NetBox flows.
- `clusters.py`: Cluster synchronization service placeholder module.
- `devices.py`: Device synchronization service from Proxmox nodes to NetBox.
- `virtual_machines.py`: Virtual machine synchronization service placeholder module.

## Key Data Flow and Dependencies

- devices.py implements node-to-device synchronization and journal tracking.
- virtual_machines.py and clusters.py are placeholders for future extraction of VM and cluster sync logic.

## Extension Guidance

- Keep sync routines idempotent where possible to support repeated runs.
- Emit structured errors with ProxboxException for route-level handling.
