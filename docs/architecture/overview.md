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
  - `/pbs/*`, `/pdm/*`, `/ceph/*`, and `/ceph/v2/*` - optional sidecar service routes. These mount by default when imports succeed, or selectively when `PROXBOX_FEATURES` is set to `pbs`, `pdm`, and/or `ceph`.
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
`/api/plugins/proxbox/sync-state/*`. `proxbox-api` writes these rows
additively during sync while continuing to write the legacy custom fields:

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

During the migration window, `proxbox-api` also reads sidecars first for
custom-field-dependent state. VM identity and orphan-sweep last-run checks use
the typed sidecar rows before falling back to the legacy `cf_*` filters.
Role-ownership snapshots remain legacy-CF-only because the VM sidecar model has
no role ownership field. This lets a normal re-sync re-adopt existing NetBox
VMs after Proxbox custom fields have been removed, while preserving
compatibility with older plugin builds. Full custom-field retirement remains a
later migration item.

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
