# proxbox_api/routes/dcim Directory Guide

## Purpose

Endpoints that synchronize and expose DCIM entities in NetBox.

## Modules and Responsibilities

- `__init__.py`: DCIM route handlers for device and interface synchronization.
  - `GET /devices/create`: creates NetBox devices from Proxmox nodes (returns JSON when complete).
  - `GET /devices/create/stream`: SSE streaming variant. Emits per-device `step` events via `WebSocketSSEBridge` while `create_proxmox_devices(...)` runs with `use_websocket=True`.
  - `GET /devices/{node}/interfaces/create`: creates interfaces for a specific device/node.
  - `GET /devices/interfaces/create`: sync all node interfaces across all clusters (JSON response).
  - `GET /devices/interfaces/create/stream`: SSE streaming variant for all-node interface sync.
  - `create_interface_and_ip()`: helper that reconciles interface and IP address objects for a node.

## Key Data Flow and Dependencies

- Consumes Proxmox-derived dependencies and sync services to create devices and interfaces.
- Depends on local netbox-sdk compatibility wrappers for creation and serialization.
- The `/devices/create/stream` endpoint uses `asyncio.create_task` to run the sync in the background, iterates `bridge.iter_sse()` for live progress frames, then awaits the task for the final result.

## Extension Guidance

- Keep endpoint orchestration simple; place long-running sync logic in services/sync.
- Preserve response_model declarations to maintain API contracts.
- When adding stream endpoints, use `WebSocketSSEBridge` and `StreamingResponse` with `media_type="text/event-stream"` and headers `Cache-Control: no-cache`, `X-Accel-Buffering: no`. Do not set `Connection: keep-alive`.
