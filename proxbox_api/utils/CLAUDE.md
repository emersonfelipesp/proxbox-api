# proxbox_api/utils Directory Guide

## Purpose

Utility helpers shared across synchronization workflows (SSE streaming, status HTML).

## Modules and Responsibilities

- `__init__.py`: Re-exports `return_status_html` from the sibling flat module `proxbox_api/utils.py` (legacy layout).
- `streaming.py`: Server-Sent Event (SSE) helpers for streaming sync progress to HTTP clients.
  - `sse_event(event, data)`: serializes one SSE frame as `event: <name>\ndata: <json>\n\n`.
  - `WebSocketSSEBridge`: compatibility bridge that accepts websocket-style `send_json(payload)` calls from sync services and converts them into SSE frames via an internal `asyncio.Queue`. Key methods:
    - `send_json(payload)`: queues an SSE `step` event with normalized `step`, `status`, `message`, `rowid`, and original `payload` fields.
    - `emit(event, data)`: queues any custom SSE event.
    - `close()`: signals end of stream.
    - `iter_sse()`: async iterator that yields serialized SSE frames until closed.
  - Normalization logic extracts `rowid` from `payload.data.rowid` or `payload.data.name`, derives `status` from `payload.data.completed` or `payload.data.error`, and builds human-readable messages like `Processing device <rowid>` or `Synced virtual_machine <rowid>`.

## Key Data Flow and Dependencies

- `streaming.py` is consumed by route handlers in `app/full_update.py`, `routes/dcim/`, and `routes/virtualization/virtual_machines/` to produce SSE streaming responses.
- Status HTML helpers live in `proxbox_api/utils.py` and are imported via this package.

## Extension Guidance

- Keep utility code generic and free from route-specific assumptions.
- When adding new SSE events, emit through `WebSocketSSEBridge` rather than raw `yield sse_event(...)` to preserve payload normalization.
