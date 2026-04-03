# WebSocket API Reference

`proxbox-api` exposes WebSocket endpoints for streaming sync progress and command execution feedback.

## `GET /` (WebSocket)

Endpoint:

- `ws://<host>:<port>/`

Behavior:

- Accepts connection.
- Sends an incremental message counter every 2 seconds.

Use case:

- Basic connectivity check.

## `GET /ws/virtual-machines` (WebSocket)

Endpoint:

- `ws://<host>:<port>/ws/virtual-machines`

Behavior:

- Accepts connection and sends welcome text.
- Triggers VM synchronization workflow (`create_virtual_machines`).
- Emits JSON progress events while VM sync runs when websocket mode is enabled in the flow.

Use case:

- Monitor VM sync lifecycle in near real-time.

## `GET /ws` (WebSocket)

Endpoint:

- `ws://<host>:<port>/ws`

Behavior:

- Accepts connection and listens for command text.
- Supported commands:
  - `Full Update Sync`
  - `Sync Nodes`
  - `Sync Virtual Machines`
- Runs the corresponding sync tasks and streams status messages.

Invalid command behavior:

- Returns guidance with valid command list.

## Notes

- WebSocket flows depend on a valid NetBox endpoint and Proxmox sessions.
- Long-running operations may create sync-process records and journal entries in NetBox plugin objects.
- Progress payloads are normalized into `step`, `error`, and `complete` message frames by the shared streaming bridge used across HTTP and WebSocket transports.

## Error Handling

WebSocket endpoints employ the same error handling and structured logging as HTTP endpoints:

### Message Frame Types

- **`step`**: Regular progress update with operation context
- **`error`**: Error occurred during operation - contains error details and operation context
- **`complete`**: Final status frame with success/failure summary

### Error Context in Messages

All error messages include:

- **operation**: The sync operation name (device_sync, vm_sync, etc.)
- **phase**: Current operation phase (filtering, creation, validation, etc.)
- **step/status**: Current step and status indicator
- **error**: Error message or exception class
- **detail**: Additional error details for debugging

### Retry and Recovery

- Transient network errors trigger automatic retry with exponential backoff
- Permanent errors are reported with full context for debugging
- All operations maintain detailed logs accessible via the NetBox plugin UI

See `docs/development/troubleshooting.md` for common error scenarios and recovery steps.
