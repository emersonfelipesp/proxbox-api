# Synchronization Workflows

This page explains major synchronization workflows between Proxmox and NetBox.

## Full update flow

HTTP endpoint:

- `GET /full-update`

High-level sequence:

1. Create NetBox sync-process record.
2. Sync Proxmox nodes into NetBox devices.
3. Sync Proxmox virtual machines into NetBox VMs.
4. Mark sync-process as completed and store runtime.

## Virtual machine sync flow

Primary endpoint:

- `GET /virtualization/virtual-machines/create`

Core behavior:

- Reads cluster resources from Proxmox sessions.
- Resolves VM configs per VM (`qemu`/`lxc`).
- Builds normalized NetBox payload.
- Creates dependencies (cluster, device, role) as needed.
- Creates VM interfaces and IP addresses when possible.
- Writes journal entries for auditability.

## Backup sync flow

Endpoints:

- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`

Core behavior:

- Discovers backup content in Proxmox storage.
- Maps backups to NetBox VMs.
- Creates backup objects under NetBox plugin model.
- Handles duplicate detection.
- Optional deletion of backups missing in Proxmox source.

## SSE streaming mode

Each sync flow has a corresponding `/stream` endpoint that emits Server-Sent Events in real time:

- `GET /full-update/stream`
- `GET /dcim/devices/create/stream`
- `GET /virtualization/virtual-machines/create/stream`

How it works:

1. The stream endpoint creates a `WebSocketSSEBridge` instance.
2. The sync service is called with `use_websocket=True` and the bridge as the `websocket` argument.
3. As the sync service processes each object, it calls `await websocket.send_json(...)` with per-object progress.
4. The bridge converts each websocket payload into an SSE `step` event with normalized fields (`step`, `status`, `message`, `rowid`, `payload`).
5. The stream endpoint iterates `bridge.iter_sse()` and yields each SSE frame to the HTTP client.
6. On completion, the bridge is closed and a final `complete` event is emitted.

This provides granular progress like:

- `Processing device pve01`
- `Synced device pve01`
- `Processing virtual_machine vm101`
- `Synced virtual_machine vm101`

## WebSocket mode

The `/ws` websocket endpoint provides interactive sync with the same per-object progress, but over a bidirectional WebSocket channel. The `full-update` command triggers the same sync logic but sends JSON messages directly to the websocket client.

## Tracking and observability

- Sync process records are created in NetBox plugin objects.
- Journal entries are written with summary and errors.
- WebSocket and SSE workflows provide interactive/real-time status output.

## Failure handling

- Domain errors are raised via `ProxboxException` and returned as structured JSON by app-level handler.
- Unhandled exceptions are caught by the global exception handler and returned as structured JSON with status 500.
- Route handlers perform best-effort continuation in certain batch loops.
- In SSE streaming mode, errors are emitted as `event: error` frames followed by a final `event: complete` with `ok: false`.
