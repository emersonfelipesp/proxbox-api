# WebSocket API Reference

`proxbox-api` exposes WebSocket endpoints for streaming sync progress and command execution feedback.

## `GET /` (WebSocket)

Endpoint:

- `ws://<host>:<port>/`

Behavior:

- Accepts connection.
- Sends incremental message counter every 2 seconds.

Use case:

- Basic connectivity check.

## `GET /ws/virtual-machines` (WebSocket)

Endpoint:

- `ws://<host>:<port>/ws/virtual-machines`

Behavior:

- Accepts connection and sends welcome text.
- Triggers VM synchronization workflow (`create_virtual_machines`).
- Emits JSON progress events while VM sync runs (when websocket mode is enabled in flow).

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
- Runs corresponding sync tasks and streams status messages.

Invalid command behavior:

- Returns guidance with valid command list.

## Notes

- WebSocket flows depend on a valid NetBox endpoint and Proxmox sessions.
- Long-running operations may create sync-process records and journal entries in NetBox plugin objects.
