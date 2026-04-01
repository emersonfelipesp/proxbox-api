# proxbox_api/utils Directory Guide

## Purpose

Utility helpers shared across synchronization workflows.

## Current Modules

- `__init__.py`: Re-exports `return_status_html` from `status_html.py`.
- `error_handling.py`: Shared error handling helpers.
- `netbox_helpers.py`: NetBox-specific helper functions.
- `retry.py`: Retry helpers for transient requests and sync operations.
- `status_html.py`: `return_status_html(status, use_css)` for sync status badges and text in HTML responses.
- `streaming.py`: Server-Sent Event helpers for streaming sync progress to HTTP clients.
- `structured_logging.py`: Structured logging helpers.
- `sync_error_handling.py`: Sync-specific error helpers.
- `type_guards.py`: Type guard and validation helpers.
- `websocket_utils.py`: WebSocket progress and status message helpers.

## Key Data Flow and Dependencies

- `streaming.py` is consumed by route handlers in `app/full_update.py`, `routes/dcim/`, and `routes/virtualization/virtual_machines/` to produce SSE streaming responses.
- `return_status_html` is used by virtualization sync routes and services.

## Extension Guidance

- Keep utility code generic and free from route-specific assumptions.
- When adding new SSE events, emit through `WebSocketSSEBridge` rather than raw `yield sse_event(...)` to preserve payload normalization.
