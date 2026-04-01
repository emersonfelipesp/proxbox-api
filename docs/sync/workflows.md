# Synchronization Workflows

This page explains major synchronization workflows between Proxmox and NetBox.

## Full update flow

HTTP endpoint:

- `GET /full-update`

High-level sequence:

1. Create NetBox sync-process record.
2. Sync Proxmox nodes into NetBox devices.
3. Sync Proxmox storages into NetBox plugin storage records.
4. Sync Proxmox virtual machines into NetBox VMs.
5. Sync virtual disks for discovered VMs.
6. Sync VM backups.
7. Sync VM snapshots.
8. Mark sync-process as completed and store runtime.

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
- `GET /virtualization/virtual-machines/backups/all/create/stream`

Core behavior:

- Discovers backup content in Proxmox storage.
- Maps backups to NetBox VMs.
- Creates backup objects under NetBox plugin model.
- Handles duplicate detection.
- Optional deletion of backups missing in Proxmox source.

## Snapshot sync flow

Endpoints:

- `GET /virtualization/virtual-machines/snapshots/create`
- `GET /virtualization/virtual-machines/snapshots/all/create`
- `GET /virtualization/virtual-machines/snapshots/all/create/stream`

Core behavior:

- Discovers snapshots for NetBox VMs mapped to Proxmox VM IDs.
- Reconciles snapshot objects in NetBox plugin model.
- Resolves related storage records when possible.

## Storage sync flow

Endpoints:

- `GET /virtualization/virtual-machines/storage/create`
- `GET /virtualization/virtual-machines/storage/create/stream`

Core behavior:

- Discovers Proxmox storage definitions.
- Reconciles NetBox plugin storage records used by backup/snapshot flows.

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

Comprehensive error handling is implemented via decorators and validation utilities:

### Error Validation

- **NetBox responses** are validated to ensure they contain required fields (e.g., `id`) before processing
- **Proxmox responses** are validated against Pydantic models for type safety
- Invalid responses raise typed exceptions (`NetBoxAPIError`, `ProxmoxAPIError`, etc.)

### Sync Error Hierarchy

Custom exception types provide detailed context:

- `VMSyncError`: Virtual machine sync failures
- `DeviceSyncError`: Node/device sync failures
- `StorageSyncError`: Storage definition failures
- `NetworkSyncError`: Network interface/VLAN failures
- Base: `SyncError` for generic sync operation failures

### Retry and Resilience

- Decorators apply exponential backoff retry logic for transient failures
- Configurable retry counts and backoff intervals
- Failed attempts are logged with context before retry
- Final failures bubble up with full error context

### Structured Logging

All sync operations use structured logging for observability:

- **Phase logging**: Each distinct phase (filtering, validation, creation) emits logs with operation and phase context
- **Resource logging**: Per-object events are logged with resource ID, type, and status
- **Completion logging**: Sync results logged with success/failure counts and elapsed time
- **Error logging**: Failures include exception details, stack traces, and full operation context

### Response Handling

- Domain errors are raised via `ProxboxException` and returned as structured JSON by app-level handler
- Unhandled exceptions are caught by the global exception handler and returned as structured JSON with status 500
- Route handlers perform best-effort continuation in certain batch loops
- In SSE streaming mode, errors are emitted as `event: error` frames followed by a final `event: complete` with `ok: false`

For details on error handling implementation, see `proxbox_api/utils/sync_error_handling.py` and `proxbox_api/utils/structured_logging.py`.
