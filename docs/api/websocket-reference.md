# WebSocket API Reference


The synchronization and SSH behavior below requires explicit
`PROXBOX_EXECUTION_MODE=legacy`. The default `rpc_only` closes `/ws`,
`/ws/virtual-machines`, and `/ssh/sessions/{session_id}/ws` with code 1008
before effects, even for a valid old ticket. Legacy synchronization requires
an API-key JSON frame within ten seconds before NetBox token acquisition,
Proxmox connections, collectors, or tag writes. Legacy SSH authenticates its
one-use ticket before stored credential configuration is acquired. The counter
WebSocket keeps its independent authentication and behavior. See
[Interactive RPC-Only Boundary](../operations/interactive-rpc-boundary.md)
for owned cleanup and the distinction between local and fleet cutover.

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

- Accepts connection and listens for either a legacy command text frame or a
  run-correlated JSON command envelope.
- Supported commands:
  - `Full Update Sync`
  - `Full Update`
  - `Sync Nodes`
  - `Sync Virtual Machines`
- Runs the corresponding sync tasks and streams status messages.

Legacy clients send the command string directly. This behavior and every
legacy response frame remain unchanged. Clients that discover
`capabilities.ws_run_id >= 1` from authenticated `GET /version` may instead
send this closed envelope:

```json
{"command": "Full Update", "run_id": "sync_run_20261014"}
```

`run_id` must match `^[A-Za-z0-9_-]{8,64}$`. Both fields are required, and
unknown fields are rejected without starting synchronization. An envelope is
limited to 4096 UTF-8 bytes; oversized, malformed, or excessively nested JSON
receives the usual invalid-command guidance, and the connection remains usable.
Every progress, terminal, and error frame produced for an accepted envelope
includes the same `run_id`. Existing JSON objects gain the field directly. A
command-path text message is represented as
`{"message": "...", "run_id": "..."}` for a correlated run.

After both stages of a correlated full update, the server emits exactly one
aggregate terminal after the last stage:

```json
{
  "object": "full-update",
  "end": true,
  "run_id": "sync_run_20261014",
  "status": "completed",
  "stages": {
    "devices": "completed",
    "virtual-machines": "completed"
  }
}
```

Stage values are `completed`, `skipped`, or `failed`. If device synchronization
returns no synchronized nodes, `virtual-machines` is `skipped`. A stage failure
produces a correlated `error_detail` frame and a final aggregate with
`status: "failed"`. Nonzero failure counts returned after partial device or
required VM-interface reconciliation count as stage failures. An individual
correlated command also emits `error_detail` for such a result but does not emit
an aggregate. Bare-string commands never receive the aggregate frame.

The authenticated version response advertises support additively:

```json
{"version": "<backend-version>", "capabilities": {"ws_run_id": 1}}
```

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
