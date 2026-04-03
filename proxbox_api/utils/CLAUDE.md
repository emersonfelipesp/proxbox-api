# proxbox_api/utils Directory Guide

## Purpose

Utility helpers shared across synchronization workflows.

## Current Modules

- `__init__.py`: re-exports `return_status_html` from `status_html.py`.
- `error_handling.py`: shared error handling helpers.
- `netbox_helpers.py`: NetBox-specific helper functions.
- `retry.py`: retry helpers for transient requests and sync operations.
- `status_html.py`: `return_status_html(status, use_css)` for sync status badges and text in HTML responses.
- `streaming.py`: Server-Sent Event helpers for streaming sync progress to HTTP clients.
- `structured_logging.py`: structured logging helpers.
- `sync_error_handling.py`: sync-specific error helpers.
- `type_guards.py`: type guard and validation helpers.
- `websocket_utils.py`: WebSocket progress and status message helpers.

## How These Utilities Are Used

- `streaming.py` is consumed by `app/full_update.py`, `app/websockets.py`, `routes/dcim/`, and `routes/virtualization/virtual_machines/` to produce SSE responses and bridge websocket-style progress events.
- `return_status_html` is used by virtualization sync routes and services when they need compact HTML status output.
- `retry.py` and `sync_error_handling.py` provide the retry and error-wrapping behavior used by sync services.
- `websocket_utils.py` and `structured_logging.py` support the shared progress-reporting path across stream transports.

## Extension Guidance

- Keep utility code generic and free from route-specific assumptions.
- Prefer shared helpers here over duplicating retry, formatting, or streaming logic in routes.
- When adding new SSE events, preserve the existing bridge and payload normalization rules.
