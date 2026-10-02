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

Node Device reconciliation uses the effective node name template documented in
[Configuration](../getting-started/configuration.md#node-device-name-template).
Maps are keyed by the Proxmox cluster and short node name, while NetBox writes
and lookups use the rendered name. Typed sync state always records the original
short node and cluster names, so VMs and interfaces remain attached to the
correct Device when clusters reuse a node name or a managed Device is renamed.

The node-interface stage maps Proxmox interface kinds to NetBox REST choice
values at the write boundary: `bridge`, `lag`, `virtual`, `loopback`, or
`other`. Python enum names are internal implementation details and must never
be sent in a `dcim.Interface.type` payload.

The `sync_node_interfaces` behavior flag (`GET /full-update?sync_node_interfaces=true`,
and the same query parameter on `GET /full-update/stream`) is forwarded to the
node-interface stage in both the non-streaming and streaming runs. With it set,
the stage reconciles the full `/nodes/{node}/network` topology, including a
bridge's pinned `hwaddress` option as its primary MAC address, exactly as
`GET /dcim/devices/interfaces/create?sync_node_interfaces=true` does. Without
it, the stage keeps the legacy per-interface behavior, which sets no MAC.

In the full-topology path a VLAN, IP address, or MAC write that fails does not
abort the node, and it is never reported as a clean run. Each skipped write is
reported as a warning `{"device", "interface", "kind", "reason"}` where `kind`
is `vlan`, `ip`, or `mac`, and the stage finishes degraded (HTTP 200,
`ok=true`). The warnings follow the same shape as the other degraded stages:
`GET /dcim/devices/interfaces/create` returns
`{"interfaces": [...], "count", "warnings", "degraded": true}` instead of the
plain list, the stream result and the per-node completion event carry
`warnings` plus `degraded`, and `full_update` adds them (tagged with
`"phase": "node-interfaces"`) to its top-level `warnings` and `degraded`. A
clean run returns exactly the plain list as before. When the VLAN of a VLAN
sub-interface cannot be reconciled, the interface keeps its existing NetBox
`mode` and `tagged_vlans`; they are left out of the topology patch instead of
being cleared, so a transient failure cannot erase VLAN assignments. Other
interfaces still have stale topology cleared when Proxmox removes it.

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
- In full-update mode, VM creation skips network writes and task history so the
  dedicated VM-interface, VM-IP, and task-history stages each own that work once.
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
5. After all successful VMs are known, reconcile their task history in one
   node-oriented aggregate (unless `sync_task_history=false`).

This means async is used for throughput where objects are independent, while parent-child dependencies are always awaited in sequence.

### Task-history ownership

The create and targeted create routes default `sync_task_history=true`, which
preserves standalone behavior. They pass only successfully reconciled NetBox VM
IDs to one aggregate call. Full-update explicitly sets the VM-stage flag to
`false` and runs its dedicated all-VM stage once afterward. The collector pages
each selected node archive once rather than scanning every node for every VM;
partial coverage is returned as `degraded=true`. Standalone REST converts that
owned degraded aggregate to HTTP 502 after retaining reconciled rows, while SSE
publishes the degraded phase summary. Selected NetBox ID lookups use bounded
repeated-value chunks and fail closed if any chunk cannot be read. See
[Task History Synchronization](./task-history.md).

Unselected aggregate task-history runs validate sidecar identity only within
the active `(endpoint, cluster)` session scopes. Retired endpoint-less rows from
unrelated clusters are unmanaged for that run and are skipped; malformed or
duplicate identity inside an active scope still fails closed. Explicit NetBox
VM selections remain strict regardless of scope.

### Staged-run selection: strict versus lenient ownership

The VM-scoped stages (`virtual-machines`, `virtual-disks`, `backups`,
`snapshots`, `vm-interfaces`, and `vm-ip-addresses`) resolve each VM's Proxmox
owner from its typed sync-state sidecar (endpoint, cluster, VMID, and VM type)
and, for the selected-list routes, against the live Proxmox resources. A VM
whose owner cannot be resolved is handled by an explicit selection mode that the
route chooses:

| Mode | Used by | Behavior |
|---|---|---|
| Strict | Routes that address one VM by path: `/{netbox_vm_id}/create`, `/{netbox_vm_id}/backups/create/stream`, `/{netbox_vm_id}/snapshots/create/stream`, `/{netbox_vm_id}/virtual-disks/create/stream` | Fail closed on the first VM whose ownership is unusable (HTTP 502 or an SSE `complete` with `ok=false`). There is no other VM to make progress on, and a wrong owner must never be guessed. |
| Lenient | Staged and estate runs: `netbox_vm_ids` list routes, `/all/create`, the estate `interfaces/create` and `interfaces/ip-address/create` stages, the full-backup ownership cache, and both full-update variants | Drop the VM with a `WARNING` naming the NetBox VM id and the reason, process the remaining VMs normally, and report the drop. |

Routes that do not say choose by addressing: list and estate routes are lenient
and single-VM path routes are strict, so an older orchestrating plugin that sends
no new parameter keeps working without change.

In lenient mode a VM is dropped when its sidecar is incomplete (no endpoint id,
cluster, positive VMID, or VM type), when it has more than one sidecar, when an
explicitly selected VM has no sidecar, when its cluster has no available Proxmox
source or is ambiguous across endpoints, when its sidecar endpoint disagrees
with the owner of the cluster, when no live Proxmox resource matches (for
example a guest deleted in Proxmox but still in NetBox) or several do, and when
two selected VMs claim the same endpoint/cluster/VMID/type. Every claimant of a
shared owner is dropped, because which one is right cannot be known. In an
estate scan a VM with no sidecar at all is unmanaged and is skipped silently,
without a warning.

These stay fatal in both modes because they are not per-VM ownership problems:
an unreadable or unavailable sidecar scan, an invalid VM id, and a selection
that NetBox does not return in full.

A dropped VM is never touched by the stage. It is absent from the ownership
cache, so it is neither reconciled nor covered by stale-backup or
stale-snapshot cleanup, and its existing NetBox rows are left as they are.

Dropped VMs are reported as structured warnings,
`[{"netbox_vm_id": <int>, "reason": "<text>"}]`, and the stage outcome is
`degraded=true`; the run does not fail, and if every selected VM is dropped the
stage returns an empty result with the warnings instead of raising:

- Dict results (`snapshots`, `virtual-disks`) carry `degraded` and `warnings`
  keys. The SSE `complete` and stage `step` events carry the same result.
- List results (`backups`, `vm-interfaces`, `vm-ip-addresses`) carry the
  warnings on the result. Their SSE result gains `warnings` and `degraded`
  next to `count`. Their REST response stays a bare list when clean and becomes
  `{"<stage>": [...], "count": n, "warnings": [...], "degraded": true}` when
  degraded.
- The `virtual-machines` stream reports the same `warnings` and `degraded` in
  its `complete` result. The REST `/create` route keeps returning a bare list
  when clean and, when a selected VM was dropped, returns
  `{"virtual_machines": [...], "count": n, "warnings": [...], "degraded": true}`.
  The by-id `/{netbox_vm_id}/create` routes stay strict and never degrade.
- Full-update (REST and SSE) aggregates every stage's warnings, each tagged with
  its `phase`, into the top-level `warnings` and sets `degraded=true`.

Task-history keeps its own contract and is not part of this mode: an explicitly
selected VM without identity is still fatal there, and its degraded aggregate
still raises HTTP 502 from standalone REST. See
[Task History Synchronization](./task-history.md).

A sidecar becomes incomplete when the VM sync writes its identity only while
`overwrite_vm_custom_fields` is enabled and drops a missing endpoint id. The
writer logs a warning naming the VM whenever the live endpoint, cluster, VMID,
or VM type is absent or the type is `unknown`, so the cause is visible where it
happens; what is persisted is unchanged.

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

## Orphan VM handling

The `delete_orphans` setting and `PROXBOX_DELETE_ORPHANS` environment override
control the end-of-run orphan scan. When disabled, the scan does not query or
mutate NetBox. When enabled, a QEMU VM or LXC container that was discovered by
Proxbox but not touched by the current run is updated, never deleted: the
backend sets `status=decommissioning` and adds the `proxbox-soft-deleted` tag.
Existing tags are preserved. A dry-run reports the candidates without sending
PATCH requests.

If the guest reappears in Proxmox, normal VM reconciliation clears the marker
while preserving every other tag. The paired NetBox plugin's **Soft-deleted
VMs** page is the only supported hard-delete path for these records. It is
permission-gated, applies the marker and status filter on both selected and
“all matching” bulk operations, and requires the operator to confirm deletion
in NetBox. The operation removes NetBox inventory only; it never calls Proxmox.

### Running the sweep from a staged sync

The full-update routes run the sweep themselves. A caller that drives each stage
separately, such as the paired NetBox plugin, must call the standalone sweep
after its stages:

- `GET /virtualization/virtual-machines/orphans/sweep`
- `GET /virtualization/virtual-machines/orphans/sweep/stream`

`run_id` is required and must be the same value the caller passed to the VM
stage, because it is the run ID stamped into each reconciled VM's sync-state
sidecar; a VM stamped with any other run ID is an orphan candidate. Optional
`dry_run=true` previews the sweep. `endpoint_ids` or `proxmox_endpoint_ids`
(comma-separated, the alias wins) restrict the sweep to VMs owned by those
Proxmox endpoints, and `vm_stage_failed=true` skips it. A live sweep (not
`dry_run`) must name an endpoint scope: without one the route answers HTTP 422,
because `run_id` and `vm_stage_failed` are unverified caller claims and an
unscoped live sweep could otherwise soft-delete every managed VM. A dry run may
stay unscoped. The route reads the
`delete_orphans` setting exactly as full-update does, so a disabled setting
returns `enabled=false` without scanning or patching. The SSE variant emits the
usual `step`, item progress and `complete` events, and the sweep result is the
`result` of the terminal event.

The sweep is deliberately conservative, and every result carries a
`skipped_reason` (`null` when the sweep ran):

| `skipped_reason` | Meaning |
|---|---|
| `disabled` | `delete_orphans` is off and the request was not a dry run. |
| `vm_stage_failed` | The VM stage reported failed VMs. A live VM that failed to reconcile is not stamped with the run and would otherwise look orphaned. |
| `sidecar_unavailable` | The sync-state sidecar API is missing (older plugin), so orphan state cannot be verified. |
| `sidecar_read_failed` | The sidecar read failed transiently, so orphan state cannot be verified. |
| `run_not_found` | No in-scope sidecar carries the given `run_id`, so the run is not proven to have happened. Checked before any PATCH or tag creation, for the standalone route and full-update alike. |
| `live_inventory_unavailable` | The standalone live sweep could not fetch the live Proxmox guest inventory for every in-scope endpoint (a session failed, an endpoint in scope had no session, or the fetch errored), so absence from Proxmox cannot be confirmed. |

Because `vm_stage_failed` and `run_id` are caller claims, the sweep also verifies
against Proxmox itself: a candidate is soft-deleted only when its guest (cluster
name, vmid, and type) is confirmed absent from the live cluster resources of the
in-scope sessions. The standalone live sweep fetches that inventory from the
sessions selected by `endpoint_ids` and fails closed with
`live_inventory_unavailable` when any of it is missing; full-update fetches a fresh
inventory at the sweep boundary (not the snapshot taken at request start, so a guest
created mid-run is seen) and fails closed the same way. A guest resource row whose
type or vmid cannot be determined (derived from ids such as `qemu/123` when fields
are missing) also makes the inventory unavailable. A candidate still present is skipped and
logged (`still_present_in_proxmox`), and one whose sidecar lacks the cluster name,
vmid, or type is skipped too (`identity_incomplete`). Dry runs do not fetch the
inventory.

Immediately before each PATCH the sweep re-reads the VM and builds the tag list from
its fresh tags plus the marker, so tags added since discovery are preserved. It skips
with `vm_unreadable` when the VM cannot be read and with `already_swept` when the
marker is already present. NetBox offers no atomic tag add, so a very small window
between that read and the PATCH remains.

A skipped sweep sends no PATCH and never creates the marker tag. Immediately
before each PATCH the sweep re-reads that VM's sidecar and skips the VM (counted
as skipped, reported as `restamped`) when the sidecar now carries this run's ID or
changed since discovery, so a VM synchronized between discovery and the PATCH is
not marked. NetBox offers no compare-and-set, so this narrows the race window but
is not an atomic guard. A VM that reappears is re-adopted by the bulk VM stage,
which removes the marker and restores the status in one PATCH. Scope is
attributed through each sidecar's `proxmox_endpoint_raw_id`: when a scope is
given, a sidecar without a valid endpoint ID is never a candidate. The
standalone live sweep therefore always needs the same endpoint IDs the caller's
run used (an unnarrowed full update is the only unscoped live sweep). A full update that was narrowed with
`endpoint_ids`, `proxmox_endpoint_ids`, `name`, `domain` or `ip_address` derives
the scope from the Proxmox sessions it actually used, and reports a failed VM
stage the same way.

The endpoint IDs are the same IDs the VM stage stamps into each sidecar (the
Proxmox session's endpoint ID), so pass the values the stage requests used. The
VM stage's SSE `complete` result carries only `count`; a staged caller reads the
failure count from the `failed` field of the `virtual-machines` phase summary.

### Decommissioned VMs in later stages

The virtual disk, snapshot, VM interface and VM IP address stages skip VMs whose
status is `decommissioning` or that carry the `proxbox-soft-deleted` tag, so
soft-deleted guests are not queried in Proxmox on every run. Each stage logs one
INFO line with the number skipped. The VM stage itself still processes them, so
a guest that reappears is re-adopted. When the disk stage cannot find a guest in
Proxmox (for example a stale record that is not yet marked) it logs a warning
and counts the VM as skipped instead of logging an error.

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
Pydantic schema, and uses it to seed user-managed NetBox fields such as
description and tags before the normal Proxmox-derived payload merges in. The
parsing logic is centralized in
`proxbox_api/proxmox_to_netbox/description_metadata.py` and locked in by
`tests/test_description_metadata.py`. Invalid JSON or schema violations are
logged but do not fail the sync — the sync falls back to the raw description
string.

### Proxbox sync-state sidecars

Sync writes reflection values into netbox-proxbox typed sidecars under
`/api/plugins/proxbox/sync-state/*`.

Mirrored write sites:

- VM identity/reflection fields (`proxmox_vm_id`, type, status, node, cluster,
  endpoint raw id, link, agent/start flags, and `proxmox_last_updated`) are
  written to `ProxboxVirtualMachineSyncState`.
- `proxbox_last_run_id` is mirrored to the VM sidecar `last_run_id`.
- Device and cluster `proxmox_last_updated` stamps are mirrored to
  `ProxboxDeviceSyncState` and `ProxboxClusterSyncState`.
- VM interface `proxbox_bridge` is mirrored to
  `ProxboxVMInterfaceSyncState.proxbox_bridge`.
- Virtual disk `proxbox_storage_id` is mirrored to
  `ProxboxVirtualDiskSyncState.proxbox_storage`.

Each reflection sidecar payload is built from live Proxmox-derived values.
Reflection fields follow the matching `overwrite_*_custom_fields` flag.
Ownership evidence such as
`proxmox_vm_name` and `proxmox_last_synced_role_id` is written independently of
that flag after successful VM reconciliation. Reflection-only sidecar writes
remain best-effort. A role snapshot that accompanies a managed role change is
correctness-critical: it is retried three times and, if still unsuccessful,
is authoritatively re-read. A confirmed commit is accepted despite the lost
response; otherwise both the previous role and previous snapshot are restored
and verified before the VM is marked failed. This prevents either half of the
pair from misclassifying the next pass as an operator edit. Deploy the
netbox-proxbox schema/API addition before this backend consumer.

VM identity lookups query
`/api/plugins/proxbox/sync-state/virtual-machines/` by `proxmox_vm_id` and
endpoint. Orphan sweep reads `last_run_id` from the VM sidecar, so a VM touched
by the current run is not deleted. Role ownership is read from
`ProxboxVirtualMachineSyncState.proxmox_last_synced_role_id`. Full/bulk sync
loads typed role snapshots once per pass, then the
engine-neutral dispatch policy applies the same decision after either Python or
Rust queue construction. Individual and sidecar-adoption paths call the same
truth table. A missing snapshot captures the current role without changing it;
a role that differs from its snapshot is preserved when overwrite is disabled;
and a role still matching its snapshot may roll forward with a changed managed
default. Unavailable, transiently failed, or conflicting snapshot reads are not
treated as a first-sync absence: the current role is preserved and no ownership
snapshot is claimed.

## Backup Sync Flow

Endpoints:

- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`
- `GET /virtualization/virtual-machines/backups/all/create/stream`
- `GET /virtualization/virtual-machines/{netbox_vm_id}/backups/create/stream`

Core behavior:

- Discovers backup content in Proxmox storage.
- Maps backups to NetBox VMs.
- Creates backup objects under the NetBox plugin model.
- Handles duplicate detection.
- Optionally deletes backups missing from the Proxmox source when
  `delete_nonexistent_backup=true`.

Targeted routes and `netbox_vm_ids` selections resolve each NetBox VM to its
exact `(Proxmox endpoint ID, normalized cluster name, Proxmox VMID)` owner.
Discovery queries only that endpoint and cluster; it never widens the selected
scope to another endpoint that happens to reuse the VMID. Missing ownership, an
unavailable owner session, or multiple selected VMs claiming the same identity
fail closed instead of guessing. Reconciliation then keys each backup by its
owning NetBox VM plus `volume_id`, so identical volume IDs owned by different
VMs remain independent.

Stale deletion is limited to VMs whose owning endpoint/cluster discovery
completed successfully. Any failed node/storage discovery task makes the run
partial and suppresses the backup deletion pass. Conversely, a fully successful
discovery that finds zero backups is authoritative and may remove stale rows,
but only for owner-covered VMs in the requested scope.

## Snapshot Sync Flow

Endpoints:

- `GET /virtualization/virtual-machines/snapshots/create`
- `GET /virtualization/virtual-machines/snapshots/all/create`
- `GET /virtualization/virtual-machines/snapshots/all/create/stream`
- `GET /virtualization/virtual-machines/{netbox_vm_id}/snapshots/create/stream`

Core behavior:

- Discovers snapshots for NetBox VMs mapped to Proxmox VM IDs.
- Reconciles snapshot objects in the NetBox plugin model.
- Resolves related storage records when possible.

Targeted routes and `netbox_vm_ids` selections preserve the exact NetBox VM,
Proxmox endpoint, cluster, and VMID ownership scope. Only the matching endpoint
session may be queried. A missing or ambiguous owner session, or an unresolved
node, fails closed for that VM without falling back to another endpoint with the
same VMID. Snapshot reconciliation also includes the owning NetBox VM in its
lookup identity, preventing cross-owner patches when names and VMIDs collide.

With `delete_nonexistent_snapshot=true`, stale cleanup is owner-scoped and is
enabled for a VM only after its snapshot discovery completed successfully. A
partial endpoint, node, or fetch failure suppresses destructive cleanup for that
owner. A fully successful empty discovery may remove stale snapshots for that
exact NetBox VM; snapshots owned by VMs outside the proven-complete scope are
not touched.

## Storage Sync Flow

Endpoints:

- `GET /virtualization/virtual-machines/storage/create`
- `GET /virtualization/virtual-machines/storage/create/stream`

Core behavior:

- Discovers Proxmox storage definitions.
- Reconciles NetBox plugin storage records used by backup and snapshot flows.

## SDN Sync Flow

Endpoint:

- `GET /proxmox/sdn/create/stream`

Core behavior:

- Reads Proxmox SDN controllers, zones, VNets, VNet subnets, fabrics, route
  maps, prefix lists, node zone content, bridges, MAC-VRF, and IP-VRF rows.
- Maps EVPN and VXLAN VNets into NetBox `vpn.L2VPN` records. EVPN
  `rt-import` values are reconciled as `ipam.RouteTarget` import targets.
- Maps valid SDN subnet CIDRs into NetBox `ipam.Prefix` records.
- Creates `vpn.L2VPNTermination` records only when runtime rows expose an
  explicit NetBox target or an unambiguous VLAN id. Existing terminations for a
  different L2VPN are left untouched and recorded as binding conflicts.
- Stores Proxmox-specific SDN metadata and raw payloads in `netbox-proxbox`
  plugin inventory endpoints.
- When `sync_mode_sdn_bgp=always` or `bootstrap_only`, projects BGP
  fabrics/controllers, resolvable sessions, route maps, prefix lists, and
  validated communities into the optional `netbox_bgp` plugin. Missing
  `netbox_bgp` APIs or unresolved session IP/ASN references are recorded as
  skipped warnings rather than failing the SDN stream.
- Treats missing or unsupported SDN Proxmox API paths as skipped warnings so
  older clusters do not fail the sync stream.

The route never writes SDN configuration back to Proxmox. It is intended to be
called by the NetBox plugin's optional `sdn` stage after VM interface and IP
address stages have already run.

## SSE Streaming Mode

Each sync flow has a corresponding `/stream` endpoint that emits Server-Sent Events in real time:

- `GET /full-update/stream`
- `GET /dcim/devices/create/stream`
- `GET /virtualization/virtual-machines/create/stream`
- `GET /proxmox/sdn/create/stream`

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

- **Dual VM interface model** — the default
  `vm_interface_sync_strategy=guest_os_model` keeps the core NetBox
  `virtualization.VMInterface` named from the Proxmox config (`net0`, `net1`,
  ...). When guest-agent data is available, proxbox-api additionally upserts
  netbox-proxbox plugin `GuestVMInterface` rows named from the guest OS
  (`ens18`, `eth0`, ...) and links their address rows to the same core
  `ipam.IPAddress` IDs already reconciled on the core VMInterface. It never
  creates duplicate IPAM records for the guest side. Older netbox-proxbox
  releases without the guest endpoints return 404; those plugin writes are
  logged and skipped without failing core interface/IP sync.
- **Deprecated legacy rename** — `vm_interface_sync_strategy=legacy_rename`
  preserves the previous behavior where `use_guest_agent_interface_name=true`
  renames the core VMInterface from `net0` to the guest OS name. The backend
  logs a deprecation warning for this mode.
- **Dedicated timeout with one retry** — the guest-agent call uses
  `PROXBOX_GUEST_AGENT_TIMEOUT` only (default 15 s, range 1–600; not a NetBox plugin setting)
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
