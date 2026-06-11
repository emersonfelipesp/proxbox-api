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
- Duplicate VM names within a single NetBox cluster are resolved deterministically before the operation queue is built. See [VM Name Collision Resolver](./name-collision-resolver.md).

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

### Two-Phase Full-Update Fetch

In full-update mode the VM batch runs in two distinct phases so that the
concurrency semaphore never holds a Proxmox HTTP response hostage while
unrelated CPU or NetBox work runs:

1. **Fetch phase** — every VM's Proxmox config is fetched first in a tight async
   batch. The fetch semaphore (`PROXBOX_VM_SYNC_MAX_CONCURRENCY`) guards *only*
   the Proxmox `get_vm_config` call, so pending HTTP responses are drained
   promptly.
2. **Process phase** — fetched configs are turned into desired NetBox state.
   The synchronous, CPU-bound work (Pydantic `model_validate`, NetBox payload
   building) is offloaded with `asyncio.to_thread` and runs from in-memory data.

Before this split, a single semaphore slot spanned fetch + validation + NetBox
calls + payload building; while slots were busy with CPU or NetBox work the
event loop could not drain in-flight Proxmox responses, so the session-level
request timeout fired falsely and produced spurious `ProxmoxTimeoutError`
failures on clusters with many VMs. Per-VM failures stay isolated in both
phases (a failed fetch or prepare increments the failure count and the rest of
the batch proceeds), and a phase-timing log line reports `fetch_ms`,
`process_ms`, and the fetch-failure count.

### Concurrent VM Operation Dispatch

After the operation queue is classified (`CREATE / GET / UPDATE`), all operations
are dispatched concurrently via `asyncio.gather`, bounded by an
`asyncio.Semaphore` whose width comes from `PROXBOX_NETBOX_WRITE_CONCURRENCY`
(plugin key `netbox_write_concurrency`, default 8).

This replaces the previous serial batch-loop that processed one VM at a time in
sequential batches. With the semaphore model:

- Up to `netbox_write_concurrency` VM operations run concurrently in NetBox.
- All remaining operations queue behind the semaphore and start as slots free up.
- Per-VM failure isolation is unchanged: a failed VM's slot is released
  immediately so the rest of the queue proceeds without blocking.

**Sizing the write concurrency vs. the connection pool:**

The write semaphore width multiplied by `PROXBOX_NETBOX_MAX_CONCURRENT` and the
uvicorn worker count determines the peak NetBox write connections. A safe rule
of thumb is to keep `netbox_write_concurrency` below the NetBox PostgreSQL
connection limit divided by `uvicorn_workers`:

```
safe_write_concurrency ≤ (netbox_max_connections / uvicorn_workers) - 2
```

For a default NetBox install with 20 connections and 4 workers, `4` is a
conservative write concurrency. With PgBouncer fronting PostgreSQL the ceiling
is higher — see the
[PostgreSQL connection pool guide](../getting-started/configuration.md#netbox-postgresql-connection-pool).

### Parallel Cluster Dependency Precomputation

Before any VM operations begin, proxbox-api resolves per-cluster NetBox
dependencies (cluster type, site, tenant, cluster object, and node devices).
These dependencies were historically processed one cluster at a time in a
for-loop.

They are now precomputed with `asyncio.gather` across all clusters so the
dependency preflight for cluster B starts while cluster A is still resolving:

1. **Within each cluster**, `_ensure_cluster_type`, `_ensure_site`, and
   `_resolve_tenant` are mutually independent and are gathered in parallel,
   followed sequentially by `_ensure_cluster` (which depends on all three).
2. **Across clusters**, all cluster coroutines are gathered in one
   `asyncio.gather` call with `return_exceptions=True`; the first
   `BaseException` is re-raised so the outer handler can wrap it as a
   `ProxboxException`.
3. Node device ensures remain sequential **within** a cluster because each
   device depends on the cluster id resolved in the step above.

This reduces wall-clock preflight time roughly proportionally to the number of
clusters — a 5-cluster environment that previously took 5× the single-cluster
preflight time now takes approximately 1× the slowest cluster's preflight.

### Sync Modes (VM and VM template)

The plugin forwards `sync_mode_vm` and `sync_mode_vm_template` query parameters
(`always` / `bootstrap_only` / `disabled`, default `always`) on each VM stage
request, and the backend enforces per-record filtering: a Proxmox resource with
a truthy `template` field is governed by `sync_mode_vm_template`, every other
QEMU/LXC resource by `sync_mode_vm`. A `disabled` mode skips matching resources
for the pass without counting them as failures; an unknown value falls back to
`always` with a warning so a malformed parameter never silently blocks a sync.

Filtering is applied **at the source**, before discovery and dependency
precompute, so a `disabled` mode does not create or update dependent NetBox
objects (manufacturer, device type, cluster, site, node devices, VM roles) for
VMs that will never be synced.

### Tag Preservation

When `overwrite_vm_tags=False` (the default), the VM sync merges Proxmox-derived tags with the user-managed NetBox tags already on the object instead of replacing them. The `Proxbox` tag is always retained so the plugin can identify objects it owns. Setting `overwrite_vm_tags=True` switches to a destructive replacement that drops any tags the sync did not produce. The same merge-vs-replace contract applies to the cluster, storage, node-interface, and IP tag groups via `overwrite_cluster_tags`, `overwrite_storage_tags`, `overwrite_node_interface_tags`, and `overwrite_ip_tags`. See [Overwrite Flags](./overwrite-flags.md).

### Cloud-init key reflection

For QEMU VMs that boot with cloud-init, the VM sync reflects the configured
SSH keys, user, and IP/Gateway/DNS bag into the NetBox VM's Proxbox metadata
so operators can audit cloud-init state without opening the Proxmox UI. The
mapping lives in `proxbox_api/proxmox_to_netbox/` and is covered by
`tests/test_vm_cloudinit_mapping.py`; the corresponding NetBox plugin tab
renders the same payload. Tracked under
[netbox-proxbox#363](https://github.com/emersonfelipesp/netbox-proxbox/issues/363).

### `netbox-metadata` JSON parsing from Proxmox descriptions

Operators can stash a fenced JSON block (`netbox-metadata`) inside the Proxmox
VM description. The sync extracts the block, validates it through a permissive
Pydantic schema, and uses it to seed user-managed NetBox fields (description,
tags, custom fields) before the normal Proxmox-derived payload merges in. The
parsing logic is centralized in
`proxbox_api/proxmox_to_netbox/description_metadata.py` and locked in by
`tests/test_description_metadata.py`. Invalid JSON or schema violations are
logged but do not fail the sync — the sync falls back to the raw description
string.

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

### Interface-Dense Guest Handling

VM interface sync reads guest interfaces from the QEMU guest agent
(`network-get-interfaces`). Guests with many interfaces (VRRP routers, alias
addresses) need extra care:

- **Dedicated timeout with one retry** — the guest-agent call uses
  `PROXBOX_GUEST_AGENT_TIMEOUT` (plugin key `guest_agent_timeout`, default 15s)
  rather than the short session default, and retries once on timeout because a
  single slow enumeration is often transient. proxmox-sdk has no per-call
  timeout, so the backend temporarily widens the HTTPS backend timeout for the
  duration of the agent call and restores it afterward.
- **Alias-MAC aggregation** — guest-agent alias entries named `"<parent>:<N>"`
  (e.g. `ens20:1`) share the parent NIC's MAC and carry extra addresses. They
  are merged into the parent interface (addresses deduped by
  `(ip_address, prefix)`) instead of letting the last MAC-keyed entry win, which
  previously mis-resolved interface names and dropped the parent's addresses.
  Genuinely distinct interfaces that share a MAC but are not alias-named (real
  VRRP interfaces) are preserved untouched.
- **Bulk-reconcile failures surface** — when the bulk VM-interface
  reconciliation fails, or completes with any failed records (partial failure),
  the stage now raises (and emits a failed stream frame) instead of returning an
  empty/partial success, so interfaces are never silently left missing in
  NetBox.

Per-VM dispatch is also isolated: a single VM's create/update failure is logged
and counted against the failure total for the run rather than aborting the whole
queue, so one bad VM no longer drops every VM queued after it.

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
