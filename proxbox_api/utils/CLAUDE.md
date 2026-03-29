# proxbox_api/utils Directory Guide

## Purpose

Utility helpers and decorators shared across synchronization workflows.

## Modules and Responsibilities

- `__init__.py`: Utility package exports for decorators and status helpers.
- `sync_decorator.py`: Decorator that tracks sync process lifecycle in NetBox. Creates sync-process records in NetBox before sync starts and finalizes them (with status and runtime) on completion or error.
- `streaming.py`: Server-Sent Event (SSE) helpers for streaming sync progress to HTTP clients.
  - `sse_event(event, data)`: serializes one SSE frame as `event: <name>\ndata: <json>\n\n`.
  - `WebSocketSSEBridge`: compatibility bridge that accepts websocket-style `send_json(payload)` calls from sync services and converts them into SSE frames via an internal `asyncio.Queue`. Key methods:
    - `send_json(payload)`: queues an SSE `step` event with normalized `step`, `status`, `message`, `rowid`, and original `payload` fields.
    - `emit(event, data)`: queues any custom SSE event.
    - `close()`: signals end of stream.
    - `iter_sse()`: async iterator that yields serialized SSE frames until closed.
  - Normalization logic extracts `rowid` from `payload.data.rowid` or `payload.data.name`, derives `status` from `payload.data.completed` or `payload.data.error`, and builds human-readable messages like `Processing device <rowid>` or `Synced virtual_machine <rowid>`.

## Key Data Flow and Dependencies

- `sync_decorator.py` wraps sync functions to create and finalize sync process records.
- `streaming.py` is consumed by route handlers in `main.py`, `routes/dcim/`, and `routes/virtualization/virtual_machines/` to produce SSE streaming responses.
- `__init__.py` re-exports helper functions consumed by routes and services.

## Extension Guidance

- Keep utility code generic and free from route-specific assumptions.
- When adding decorators, ensure both success and failure paths update sync state.
- When adding new SSE events, emit through `WebSocketSSEBridge` rather than raw `yield sse_event(...)` to preserve payload normalization.
