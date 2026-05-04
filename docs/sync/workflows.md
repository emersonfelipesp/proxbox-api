# Synchronization Workflows

This page explains the major synchronization workflows between Proxmox and NetBox.

## Full Update Flow

HTTP endpoint:

- `GET /full-update`

Current execution order:

1. Sync Proxmox nodes into NetBox devices.
2. Sync Proxmox storages into NetBox plugin storage records.
3. Sync Proxmox virtual machines into NetBox VMs.
4. Sync task history records.
5. Sync virtual disks for discovered VMs.
6. Sync VM backups.
7. Sync VM snapshots.
8. Sync node interfaces and IP addresses.
9. Sync VM interfaces.
10. Sync VM IP addresses and primary IP assignment.
11. Sync replication jobs across Proxmox clusters.
12. Sync backup routines (scheduled backup job configurations).

The streaming variant at `GET /full-update/stream` emits the same stage transitions over Server-Sent Events.

## Virtual Machine Sync Flow

Primary endpoint:

- `GET /virtualization/virtual-machines/create`

Core behavior:

- Reads cluster resources from Proxmox sessions.
- Resolves VM configs per VM (`qemu` and `lxc`).
- Builds normalized NetBox payloads.
- Creates dependencies such as cluster, device, and role as needed.
- Creates VM interfaces and IP addresses when possible.
- Writes journal entries for auditability.
- In full-update mode, VM creation skips network writes so the dedicated VM interface and IP stages own that work.

### Dependency-Ordered Async Model

VM sync is async end-to-end, but not every step can run in parallel. The workflow enforces a strict dependency chain before running VM-level fan-out.

Sequential dependency preflight:

1. Ensure global parent objects exist in NetBox:
	- Manufacturer
	- Device type (depends on manufacturer)
	- Proxmox node role
2. For each cluster, ensure cluster-scoped parents:
	- Cluster type
	- Cluster
	- Site
3. For each node in the cluster, ensure device:
	- Device (depends on cluster + device type + role + site)
4. Ensure VM role objects by VM type (`qemu` and `lxc`).

After this preflight, VM operations run concurrently per VM with a semaphore limit.

Per-VM required order:

1. Fetch VM data from Proxmox (resource/config).
2. Reconcile VM in NetBox (create/patch).
3. Reconcile VM interfaces and IPs (if enabled).
4. Reconcile VM disks.
5. Reconcile VM task history.

This means async is used for throughput where objects are independent, while parent-child dependencies are always awaited in sequence.

### Parallelism Rules

Allowed in parallel:

- Different VMs in the same or different clusters, after preflight dependencies are ready.
- Interface operations for a single VM once the VM object exists.
- Disk operations for a single VM once the VM object exists.

Not allowed in parallel:

- Creating child objects before required parent objects exist.
- Reconciling NetBox VM state before Proxmox VM data is fetched.
- Creating a device before manufacturer/device type/site/cluster prerequisites exist.

### Tag Preservation

When `overwrite_vm_tags=False` (the default), the VM sync merges Proxmox-derived tags with the user-managed NetBox tags already on the object instead of replacing them. The `Proxbox` tag is always retained so the plugin can identify objects it owns. Setting `overwrite_vm_tags=True` switches to a destructive replacement that drops any tags the sync did not produce. The same merge-vs-replace contract applies to the cluster, storage, node-interface, and IP tag groups via `overwrite_cluster_tags`, `overwrite_storage_tags`, `overwrite_node_interface_tags`, and `overwrite_ip_tags`. See [Overwrite Flags](./overwrite-flags.md).

## Backup Sync Flow

Endpoints:

- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`
- `GET /virtualization/virtual-machines/backups/all/create/stream`

Core behavior:

- Discovers backup content in Proxmox storage.
- Maps backups to NetBox VMs.
- Creates backup objects under the NetBox plugin model.
- Handles duplicate detection.
- Optional deletion of backups missing from the Proxmox source.

## Snapshot Sync Flow

Endpoints:

- `GET /virtualization/virtual-machines/snapshots/create`
- `GET /virtualization/virtual-machines/snapshots/all/create`
- `GET /virtualization/virtual-machines/snapshots/all/create/stream`

Core behavior:

- Discovers snapshots for NetBox VMs mapped to Proxmox VM IDs.
- Reconciles snapshot objects in the NetBox plugin model.
- Resolves related storage records when possible.

## Storage Sync Flow

Endpoints:

- `GET /virtualization/virtual-machines/storage/create`
- `GET /virtualization/virtual-machines/storage/create/stream`

Core behavior:

- Discovers Proxmox storage definitions.
- Reconciles NetBox plugin storage records used by backup and snapshot flows.

## SSE Streaming Mode

Each sync flow has a corresponding `/stream` endpoint that emits Server-Sent Events in real time:

- `GET /full-update/stream`
- `GET /dcim/devices/create/stream`
- `GET /virtualization/virtual-machines/create/stream`

How it works:

1. The stream endpoint creates a `WebSocketSSEBridge` instance.
2. The sync service is called with `use_websocket=True` and the bridge as the `websocket` argument.
3. As the sync service processes each object, it calls `await websocket.send_json(...)` with per-object progress.
4. The bridge converts each websocket payload into an SSE `step` event with normalized fields.
5. The stream endpoint iterates `bridge.iter_sse()` and yields each SSE frame to the HTTP client.
6. On completion, the bridge is closed and a final `complete` event is emitted.

This provides granular progress like:

- `Processing device pve01`
- `Synced device pve01`
- `Processing virtual_machine vm101`
- `Synced virtual_machine vm101`

## WebSocket Mode

The `/ws` websocket endpoint provides interactive sync with the same per-object progress, but over a bidirectional WebSocket channel.
The `full-update` command triggers the same sync logic but sends JSON messages directly to the websocket client.

## Tracking and Observability

- Sync process records are created in NetBox plugin objects.
- Journal entries are written with summaries and errors.
- WebSocket and SSE workflows provide interactive, real-time status output.

## Failure Handling

Comprehensive error handling is implemented via decorators and validation utilities:

### Error Validation

- NetBox responses are validated to ensure they contain required fields before processing.
- Proxmox responses are validated against Pydantic models where typed helpers are available.
- Invalid responses raise typed exceptions such as `NetBoxAPIError` or `ProxmoxAPIError`.

### Sync Error Hierarchy

Custom exception types provide detailed context:

- `VMSyncError`: Virtual machine sync failures
- `DeviceSyncError`: Node/device sync failures
- `StorageSyncError`: Storage definition failures
- `NetworkSyncError`: Network interface and VLAN failures
- Base: `SyncError` for generic sync operation failures

### Retry and Resilience

- Retry helpers apply exponential backoff to transient failures.
- The retry behavior is configurable through `PROXBOX_NETBOX_MAX_RETRIES` and `PROXBOX_NETBOX_RETRY_DELAY`.
- Failed attempts are logged with context before retry.
- Final failures bubble up with full error context.

### Structured Logging

All sync operations use structured logging for observability:

- Phase logging: each distinct phase emits logs with operation and phase context.
- Resource logging: per-object events are logged with resource ID, type, and status.
- Completion logging: sync results include success and failure counts plus elapsed time.
- Error logging: failures include exception details, stack traces, and full operation context.

### Response Handling

- Domain errors are raised via `ProxboxException` and returned as structured JSON by app-level handlers.
- Unhandled exceptions are caught by the global exception handler and returned as structured JSON with status 500.
- Route handlers perform best-effort continuation in certain batch loops.
- In SSE streaming mode, errors are emitted as `event: error` frames followed by a final `event: complete` with `ok: false`.

For details on error handling implementation, see `proxbox_api/utils/sync_error_handling.py` and `proxbox_api/utils/structured_logging.py`.
