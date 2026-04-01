# proxbox_api/services/sync Directory Guide

## Purpose

Synchronization services responsible for NetBox object creation from Proxmox data.

## Modules and Responsibilities

- `__init__.py`: Synchronization service namespace for Proxmox to NetBox flows.
- `clusters.py`: Cluster synchronization service placeholder module.
- `devices.py`: Device synchronization service from Proxmox nodes to NetBox.
  - `create_proxmox_devices(netbox_session, clusters_status, tag, websocket=None, use_websocket=False, ...)`: main sync function. When `use_websocket=True` and `websocket` is provided, sends progress JSON via `await websocket.send_json(...)` for each device (start, success, failure). Accepts a `WebSocketSSEBridge` instance as `websocket` to produce SSE frames instead of websocket messages.
  - Internal helpers: `_ensure_cluster_type`, `_ensure_cluster`, `_ensure_manufacturer`, `_ensure_device_type`, `_ensure_device_role`, `_ensure_site`, `_ensure_device` — all use `rest_reconcile_async` for idempotent object creation.
  - `_wrap_device_phase_error(phase, error)`: wraps exceptions with phase context for better error messages.
- `virtual_machines.py`: Virtual machine helper module containing `build_netbox_virtual_machine_payload` and other mapper logic used by sync routes.

## Key Data Flow and Dependencies

- `devices.py` implements node-to-device synchronization and journal tracking.
- `virtual_machines.py` provides payload builder and normalizer helpers consumed by VM sync routes.

## Extension Guidance

- Keep sync routines idempotent where possible to support repeated runs.
- Emit structured errors with ProxboxException for route-level handling.
- When adding progress reporting, use `await websocket.send_json(...)` so both websocket and SSE streaming modes receive the same payload shape.
