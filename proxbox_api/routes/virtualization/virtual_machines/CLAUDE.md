# proxbox_api/routes/virtualization/virtual_machines Directory Guide

## Purpose

Main synchronization endpoints for virtual machines and backups.

## Modules and Responsibilities

- `read_vm.py` (included from `__init__.py`): Read and stub routes for a single VM by NetBox id.
  - `GET /{id}`: Returns the VM from NetBox; **404** when missing, **502** on NetBox/API errors (not an empty object).
  - `GET /{id}/summary`, interface-related stubs: **501 Not Implemented** with explicit `detail` until implemented.
- `__init__.py`: Virtual machine sync routes and backup workflows.
  - `GET /create`: creates NetBox virtual machines from Proxmox resources (returns JSON when complete). Supports `websocket` and `use_websocket` parameters for live progress.
  - `GET /create/stream`: SSE streaming variant. Emits per-VM `step` events via `WebSocketSSEBridge` while `create_virtual_machines(...)` runs with `use_websocket=True`.
  - `GET /backups/all/create`: creates backup objects for all discovered VMs.
  - `GET /backups/{vmid}/create`: creates backup objects for a specific VM.
  - Concurrency control: VM sync tasks are wrapped with `asyncio.Semaphore` sized by the `PROXBOX_VM_SYNC_MAX_CONCURRENCY` environment variable (default: 4).

## Key Data Flow and Dependencies

- Aggregates Proxmox cluster resources, VM configs, and NetBox object creation calls.
- Uses sync decorators and extras dependencies for process tracking and custom fields.
- Writes journal entries to NetBox for auditability of each synchronization run.
- The stream endpoint uses `asyncio.create_task` to run the sync in the background, iterates `bridge.iter_sse()` for live progress frames, then awaits the task for the final result.

## Extension Guidance

- Extract large helper blocks into service modules when adding new sync paths.
- Maintain websocket and non-websocket code paths with equivalent behavior.
- When adding stream endpoints, use `WebSocketSSEBridge` and `StreamingResponse` with `media_type="text/event-stream"` and headers `Cache-Control: no-cache`, `X-Accel-Buffering: no`. Do not set `Connection: keep-alive`.
