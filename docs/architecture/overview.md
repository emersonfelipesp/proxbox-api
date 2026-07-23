# Architecture Overview

`proxbox-api` is organized around FastAPI routing, session dependencies, sync services, and schema layers.

## High-level Layers

- API layer: `proxbox_api/main.py`, `proxbox_api/app/*`, and `proxbox_api/routes/*`
- Session layer: `proxbox_api/session/*`
- Service layer: `proxbox_api/services/*`
- Schema and enum layer: `proxbox_api/schemas/*`, `proxbox_api/enum/*`
- Persistence layer: `proxbox_api/database.py`
- Utility layer: streaming, logging, cache, retry, and error helpers

## Request-Level Caching

NetBox GET requests are cached in-memory to reduce database load during sync operations:

- **Cache location**: `proxbox_api/netbox_rest.py` provides `rest_list_async()`, `rest_first_async()`, etc.
- **TTL**: configurable via `PROXBOX_NETBOX_GET_CACHE_TTL` (default: 60 seconds)
- **Entry limit**: configurable via `PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES` (default: 4096)
- **Byte limit**: configurable via `PROXBOX_NETBOX_GET_CACHE_MAX_BYTES` (default: 52428800 = 50MB)
- **Eviction policy**: LRU (Least Recently Used) when either entry or byte limit is reached
- **Invalidation**: automatic on POST/PATCH/DELETE to related endpoints
- **Observability**: metrics available at `/cache` and `/cache/metrics/prometheus`

Cache invalidation is precise (not prefix-based): updating `/api/dcim/devices/55/` only invalidates that exact path and its parent list, not other device detail paths like `/api/dcim/devices/10/`.

## Runtime Components

- FastAPI app mounts the current route groups:
  - `/`
  - `/cache`
  - `/clear-cache`
  - `/full-update`
  - `/ws`
  - `/ws/virtual-machines`
  - `/admin`
  - `/admin/encryption` — encryption key inspection and rotation surface.
  - `/auth` — bootstrap and API-key management.
  - `/cloud/*` - NMS Cloud VM, LXC, template, image-factory, Azure VHD import, and Firecracker provisioning routes. See [Service Routes](../api/service-routes.md).
  - `/intent/*` - NetBox-to-Proxmox plan/apply/deletion-request safety routes. See [Service Routes](../api/service-routes.md).
  - `/pbs/*`, `/pdm/*`, `/ceph/*`, and `/ceph/v2/*` - optional sidecar service routes. Ceph v2 stores canonical plans, stable server-keyed endpoint revisions, hashed single-use approvals, owner-bound leased runs, append-only dispatch/task events, and provider-global task claims in SQLite; binds every Proxmox write to one exact private endpoint session and one exact operation node; strictly validates each mutation payload; reloads policy/revision/binding before every mutation; carries durability tasks through repeated cancellation; and treats missing/reused/node-inconsistent UPIDs, ambiguous legacy cross-endpoint claims, or expired leases as unknown outcomes. Mutation is default-off behind separate execution and trusted-gateway flags, while Dashboard/external apply remains closed pending durable provider authority. See [Ceph v2 Write Approval and Recovery](../operations/ceph-write-approvals.md). These routes mount by default when imports succeed, or selectively when `PROXBOX_FEATURES` is set to `pbs`, `pdm`, and/or `ceph`.
  - `/netbox`
  - `/proxmox`
  - `/proxmox/cluster/ha/*` — read-only High-Availability aggregation across configured clusters; see [Cluster HA API](../api/cluster-ha.md).
  - `/proxmox/{qemu,lxc}/{vmid}/{start,stop,snapshot,migrate}` — operational write verbs (plus DELETE-to-cancel and GET-stream for migrate). Gated by `ProxmoxEndpoint.allow_writes`. See [HTTP API Reference — VM Operational Verbs](../api/http-reference.md#vm-operational-verbs).
  - `/dcim`
  - `/virtualization`
  - `/extras`
  - `/sync/individual`
  - `/sync/active` — process-local probe for an in-flight `/full-update` run.
- Sidecar-only mode: when `PROXBOX_FEATURES` contains only optional sidecar flags (`pbs`, `ceph`, `pdm`), the core Proxmox/NetBox/sync/cloud/intent route groups are skipped and only the selected service routes mount alongside root metadata and auth.
- SQLite-backed endpoint configuration and bootstrap state.
- NetBox API access via `netbox-sdk` sync and async clients.
- Proxmox API access via `proxmox-sdk` sync SDK sessions and typed helper wrappers.
- Firecracker host-agent access via `proxbox_api.firecracker_agent.client.FirecrackerHostAgentClient`.
- Runtime-generated Proxmox live routes mounted during app lifespan startup.

## Core Data Models

### NetBox sync-state sidecars

The NetBox plugin owns typed Proxbox sync-state sidecars under
`/api/plugins/proxbox/sync-state/*`. These typed sidecars are now the
**standard** source of truth for the Proxmox-to-NetBox linkage: `proxbox-api`
writes and reads them during sync. The legacy reflection custom fields are
**deprecated** and gated behind the `custom_fields_enabled` plugin setting,
which defaults to `false` — so by default no custom fields are written, read, or
reconciled, and the sidecars stand alone. `proxbox-api` writes these rows during
sync:

- `ProxboxVirtualMachineSyncState` extends `virtualization.VirtualMachine`.
- `ProxboxDeviceSyncState` extends `dcim.Device`.
- `ProxboxClusterSyncState` extends `virtualization.Cluster`.
- `ProxboxVirtualDiskSyncState` extends `virtualization.VirtualDisk`.
- `ProxboxVMInterfaceSyncState` extends `virtualization.VMInterface`.

The sidecars carry the same synchronized data that historically lived only in
custom fields, including VM Proxmox identity, device/cluster timestamps,
VM-interface bridge links, virtual-disk storage links, and VM last-run ids.
Writes use the existing NetBox session and degrade gracefully when an older
plugin does not expose the sidecar API.

**Scope note.** `proxbox-api` populates typed sidecars for the five core object
types listed above (VM, device, cluster, virtual disk, VM interface), which hold
all Proxmox identity and linkage data. The supporting objects synced during a run
(cluster types, manufacturers, device types, device roles, sites) only ever
carried a `proxmox_last_updated` reflection timestamp in custom fields; with
`custom_fields_enabled=false` (the default) that stamp is no longer written, and
the plugin's typed sidecar models for those supporting objects are not populated
by the backend today. This is intentional — supporting objects carry no
Proxmox-to-NetBox linkage — and dropping the stamp has no effect on sync
identity, orphan detection, or reconciliation. Enable `custom_fields_enabled` if
you still need the legacy supporting-object timestamp during a transition.

`proxbox-api` reads the sidecars for custom-field-dependent state. VM identity
and orphan-sweep last-run checks use the typed sidecar rows. With
`custom_fields_enabled=false` (the default) there is **no** legacy `cf_*`
fallback — reads are sidecar-only, and because the sidecars are rebuilt from
live Proxmox data on each sync, a normal re-sync re-adopts existing NetBox VMs
even when the custom fields are already gone. Setting
`custom_fields_enabled=true` restores the legacy behavior for a transition
period (dual-writing custom fields and using the `cf_*` read fallback), and
every custom-field code path then emits a deprecation warning. Role-ownership
snapshots have no sidecar field and are only read when the flag is enabled. Full
custom-field retirement remains a later migration item; no custom-field data is
deleted while the flag exists.

### `NetBoxEndpoint`

- Fields: `name`, `ip_address`, `domain`, `port`, `token_version`, `token_key`, `token`, `verify_ssl`
- Supports both NetBox token v1 and v2 shapes.
- Includes computed `url` property for NetBox session creation.
- API-level singleton behavior is enforced by create endpoint logic.

### `ProxmoxEndpoint`

- Fields: `name`, `ip_address`, `domain`, `port`, `username`, `password`, `verify_ssl`, `token_name`, `token_value`
- `domain` is optional and `name` is unique.
- Supports either password auth or token-pair auth.

## Startup Flow

1. `create_app()` initializes the database and NetBox bootstrap state.
2. The app mounts static assets, CORS middleware, exception handlers, cache routes, full-update routes, and WebSocket routes.
3. Route packages are included for NetBox, Proxmox, DCIM, virtualization, extras, and individual sync helpers.
4. Generated Proxmox live routes are mounted during lifespan startup and can fail open unless `PROXBOX_STRICT_STARTUP` is enabled.
5. The custom OpenAPI builder embeds the generated Proxmox OpenAPI contract when one is available.

## OpenAPI Extension

`proxbox_api/openapi_custom.py` overrides FastAPI OpenAPI generation and embeds generated Proxmox OpenAPI metadata when available:

- Source file: `proxbox_api/generated/proxmox/latest/openapi.json`
- Extension fields:
  - `info.x-proxmox-generated-openapi`
  - `x-proxmox-generated-openapi`

## Sync Lifecycle

- Sync endpoints orchestrate Proxmox discovery and NetBox object creation.
- Journal entries and sync-process records are used for traceability.
- WebSocket and SSE streaming endpoints provide real-time sync progress with per-object granularity.
