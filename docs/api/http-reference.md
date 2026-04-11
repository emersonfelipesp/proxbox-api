# HTTP API Reference

This page summarizes the HTTP endpoints exposed by `proxbox-api`.

For full request and response schemas, use the runtime OpenAPI at `/docs`.

## Root and Utilities

- `GET /` - Service metadata and links.
- `GET /version` - Backend service version for external cache invalidation.
- `GET /cache` - Inspect the in-memory cache snapshot.
- `GET /clear-cache` - Clear the in-memory cache.

## Authentication (`/auth`)

All requests except bootstrap endpoints require the `X-Proxbox-API-Key` header. See [Authentication](../getting-started/authentication.md) for the full bootstrap flow and key management guide.

- `GET /auth/bootstrap-status` - Check whether first-time key registration is still needed. Auth-exempt.
- `POST /auth/register-key` - Register the first API key. Auth-exempt; fails once a key already exists.
- `POST /auth/keys` - Create a new API key. Returns the raw key value once; store it securely.
- `GET /auth/keys` - List all API keys. Key values are redacted (only metadata is returned).
- `DELETE /auth/keys/{key_id}` - Delete an API key by ID.
- `POST /auth/keys/{key_id}/activate` - Re-activate a previously deactivated key.
- `POST /auth/keys/{key_id}/deactivate` - Deactivate an active key without deleting it.

## Admin

- `GET /admin/` - HTML admin dashboard for the configured NetBox endpoint records. This route is excluded from OpenAPI.
- `GET /admin/logs` - In-memory backend log buffer with optional filters for `level`, `limit`, `offset`, `since`, and `operation_id`.
- `GET /admin/logs/stream` - SSE real-time log stream. Supports query parameters `level`, `errors_only`, `operation_id`, and `newer_than_id`.

## NetBox Routes (`/netbox`)

- `POST /netbox/endpoint` - Create the singleton NetBox endpoint.
- `GET /netbox/endpoint` - List NetBox endpoint records.
- `GET /netbox/endpoint/{netbox_id}` - Get endpoint by ID.
- `PUT /netbox/endpoint/{netbox_id}` - Update endpoint.
- `DELETE /netbox/endpoint/{netbox_id}` - Delete endpoint.
- `GET /netbox/status` - Fetch NetBox API status.
- `GET /netbox/openapi` - Fetch NetBox OpenAPI.

### NetBox singleton rule

Attempting to create a second endpoint returns HTTP 400 with:

```json
{
  "detail": "Only one NetBox endpoint is allowed"
}
```

## Proxmox Routes (`/proxmox`)

### Endpoint configuration CRUD

- `POST /proxmox/endpoints`
- `GET /proxmox/endpoints`
- `GET /proxmox/endpoints/{endpoint_id}`
- `PUT /proxmox/endpoints/{endpoint_id}`
- `DELETE /proxmox/endpoints/{endpoint_id}`

Validation rules:

- Provide `password`, or both `token_name` and `token_value`.
- `token_name` and `token_value` must be set together.
- Endpoint names must be unique.

### Session and discovery

- `GET /proxmox/sessions`
- `GET /proxmox/version`
- `GET /proxmox/`
- `GET /proxmox/storage`
- `GET /proxmox/nodes/{node}/storage/{storage}/content`
- `GET /proxmox/{top_level}` where `top_level` is one of `access`, `cluster`, `nodes`, `storage`, or `version`
- `GET /proxmox/{node}/{type}/{vmid}/config`

### Cluster, node, and replication data

- `GET /proxmox/cluster/status`
- `GET /proxmox/cluster/resources`
- `GET /proxmox/nodes/`
- `GET /proxmox/nodes/{node}/network`
- `GET /proxmox/nodes/{node}/qemu`
- `GET /proxmox/replication`

### Viewer and generated contract helpers

- `POST /proxmox/viewer/generate`
- `GET /proxmox/viewer/openapi`
- `GET /proxmox/viewer/openapi/embedded`
- `GET /proxmox/viewer/integration/contracts`
- `POST /proxmox/viewer/routes/refresh`
- `GET /proxmox/viewer/pydantic`

### Runtime-generated live proxy routes

`proxbox-api` mounts runtime-generated Proxmox proxy routes from the embedded generated OpenAPI contract under:

- `/proxmox/api2/{version_tag}/*`
- `/proxmox/api2/*` as a compatibility alias to `latest`

Behavior:

- Routes are built at startup for every generated version present under `proxbox_api/generated/proxmox/`.
- The mounted route set is cached in `proxbox_api/generated/proxmox/runtime_generated_routes_cache.json`.
- On `uvicorn --reload`, startup prefers that cache manifest so the previously mounted live route set is preserved in development.
- Routes are rebuilt on demand with `POST /proxmox/viewer/routes/refresh`.
- `POST /proxmox/viewer/routes/refresh` with no query parameters rebuilds all available generated versions.
- `POST /proxmox/viewer/routes/refresh?version_tag=8.3.0` rebuilds only that mounted version.
- The unversioned `/proxmox/api2/*` alias forwards to the `latest` generated contract.
- Request bodies and responses are validated with runtime-generated Pydantic models.
- Generated response models cover object, array, scalar, and `null` response schemas.
- For array responses whose items are objects, generation emits `{Operation}ResponseItem` plus `RootModel[list[{Operation}ResponseItem]]` so Swagger shows concrete item fields.
- Generated routes appear in FastAPI `/docs` and `/openapi.json`.
- `latest` routes are mounted before older version tags so they appear first in Swagger.
- Generated routes are prioritized ahead of older handcrafted `/proxmox/*` routes so path collisions resolve to the generated API surface.

Path parameter normalization:

- When the Proxmox viewer uses path parameter names that are not valid FastAPI identifiers, the mounted FastAPI route uses a normalized placeholder name.
- Example:
  - Proxmox contract path: `/nodes/{node}/hardware/pci/{pci-id-or-mapping}`
  - Mounted FastAPI path: `/proxmox/api2/latest/nodes/{node}/hardware/pci/{pci_id_or_mapping}`
- The upstream proxmox-sdk SDK call still uses the original Proxmox parameter name from the generated OpenAPI contract.

Version discovery:

- A version is mountable only when `proxbox_api/generated/proxmox/<version-tag>/openapi.json` exists.
- Non-version entries such as `__pycache__` and files at the root of `generated/proxmox/` are ignored.

Target selection:

- If exactly one Proxmox endpoint exists, generated routes use it automatically.
- If more than one endpoint exists, pass one of:
  - `target_name`
  - `target_domain`
  - `target_ip_address`
- `source` selects whether endpoints come from the local database or NetBox plugin records.

Typed sync integration:

- Handcrafted sync-facing routes still call Proxmox directly, but now do so through `proxbox_api/services/proxmox_helpers.py` backed by the proxmox-sdk SDK.
- That helper layer validates live proxmox-sdk payloads with the generated models in `proxbox_api/generated/proxmox/latest/pydantic_models.py` before returning data to route handlers.
- This avoids internal HTTP round-trips while keeping VM config, cluster status, cluster resources, storage listing, and node storage content aligned with the generated contract used by `/proxmox/api2/*`.

Examples of generated route shapes:

- `GET /proxmox/api2/latest/cluster/resources`
- `GET /proxmox/api2/8.3.0/nodes/{node}/qemu/{vmid}/config`
- `POST /proxmox/api2/latest/access/acl`
- `GET /proxmox/api2/cluster/resources` as the compatibility alias for `latest`

Refresh response shape:

- Top-level response: registration summary from `register_generated_proxmox_routes()` plus the message field.
- `state`: nested snapshot from `generated_proxmox_route_state()`.
- `state.mounted_versions`: the versioned route sets currently mounted in FastAPI.
- `state.alias_version_tag`: the version used by `/proxmox/api2/*`.
- `state.cache_path`: persisted manifest path used to preserve generated routes across reloads.
- `state.cache_enabled`: whether cache persistence is enabled for generated routes.
- `state.loaded_from_cache`: whether the latest registration used the persisted runtime cache.
- `state.route_count`: total generated FastAPI routes currently mounted.
- `state.versions.<tag>.route_count`: number of FastAPI routes mounted for that version.
- `state.versions.<tag>.path_count`: number of OpenAPI paths mounted for that version.
- `state.versions.<tag>.method_count`: number of HTTP operations mounted for that version.
- `state.versions.<tag>.schema_version`: the `info.version` value from the generated OpenAPI document.

Test coverage:

- `tests/test_generated_proxmox_routes.py` runs a mock-based exhaustive route suite over every generated operation for every available version plus the `latest` alias.
- `tests/test_pydantic_generator_models.py` verifies generated response models for array, scalar, `null`, and aliased object payloads.
- `tests/test_session_and_helpers.py` verifies the typed proxmox helper layer and confirms the handcrafted sync dependencies return helper-validated payloads.

## DCIM Routes (`/dcim`)

- `GET /dcim/devices`
- `GET /dcim/devices/create` - Create NetBox devices from Proxmox nodes.
- `GET /dcim/devices/create/stream` - SSE streaming variant.
- `GET /dcim/devices/{node}/interfaces/create`
- `GET /dcim/devices/interfaces/create` - Sync all node interfaces across all clusters.
- `GET /dcim/devices/interfaces/create/stream` - SSE streaming variant for all-node interface sync.

## Virtualization Routes (`/virtualization`)

- `GET /virtualization/cluster-types/create` - Stub that returns HTTP 501.
- `GET /virtualization/clusters/create` - Stub that returns HTTP 501.
- `GET /virtualization/virtual-machines/create` - Create NetBox VMs from Proxmox resources.
- `GET /virtualization/virtual-machines/create/stream` - SSE streaming variant.
- `GET /virtualization/virtual-machines/{netbox_vm_id}/create` - Create a single VM by NetBox ID.
- `GET /virtualization/virtual-machines/{netbox_vm_id}/create/stream` - SSE streaming variant for single VM sync.
- `GET /virtualization/virtual-machines/`
- `GET /virtualization/virtual-machines/{id}`
- `GET /virtualization/virtual-machines/{id}/summary` - Stub that returns HTTP 501.
- `GET /virtualization/virtual-machines/summary/example`
- `GET /virtualization/virtual-machines/interfaces/create`
- `GET /virtualization/virtual-machines/interfaces/create/stream`
- `GET /virtualization/virtual-machines/interfaces/ip-address/create`
- `GET /virtualization/virtual-machines/interfaces/ip-address/create/stream`
- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`
- `GET /virtualization/virtual-machines/backups/all/create/stream`
- `GET /virtualization/virtual-machines/{netbox_vm_id}/backups/create/stream`
- `GET /virtualization/virtual-machines/snapshots/create`
- `GET /virtualization/virtual-machines/snapshots/all/create`
- `GET /virtualization/virtual-machines/snapshots/all/create/stream`
- `GET /virtualization/virtual-machines/{netbox_vm_id}/snapshots/create/stream`
- `GET /virtualization/virtual-machines/virtual-disks/create`
- `GET /virtualization/virtual-machines/virtual-disks/create/stream`
- `GET /virtualization/virtual-machines/{netbox_vm_id}/virtual-disks/create/stream`
- `GET /virtualization/virtual-machines/storage/create`
- `GET /virtualization/virtual-machines/storage/create/stream`

## Full Update

- `GET /full-update` - Runs device sync, storage sync, VM sync, task history sync, disk sync, backup sync, snapshot sync, node interface sync, VM interface sync, VM IP sync, replication sync, and backup routine sync.
- `GET /full-update/stream` - SSE streaming variant.

## WebSocket

- `GET /` - Basic counter WebSocket for connectivity checks.
- `GET /ws/virtual-machines` - WebSocket-triggered VM synchronization.
- `GET /ws` - Command-driven WebSocket for sync orchestration.

## SSE Streaming Format

All `/stream` endpoints return `Content-Type: text/event-stream` and emit three event types:

| Event | Description |
|-------|-------------|
| `step` | Progress frame with `step`, `status`, `message`, `rowid`, and `payload`. |
| `error` | Error frame with `step`, `status: "failed"`, `error`, and `detail`. |
| `complete` | Final frame with `ok`, `message`, and optionally `result` or `errors`. |

Headers:

- `Cache-Control: no-cache`
- `X-Accel-Buffering: no`

## Sync Individual Routes (`/sync/individual`)

- `GET /sync/individual/node`
- `GET /sync/individual/vm`
- `GET /sync/individual/vm/{cluster_name}/{node}/{type}/{vmid}`
- `GET /sync/individual/cluster`
- `GET /sync/individual/interface`
- `GET /sync/individual/ip`
- `GET /sync/individual/disk`
- `GET /sync/individual/storage`
- `GET /sync/individual/snapshot`
- `GET /sync/individual/task-history`
- `GET /sync/individual/backup`
- `GET /sync/individual/replication`
- `GET /sync/individual/backup-routines`

## Extras Routes (`/extras`)

- `GET /extras/extras/custom-fields/create`

This endpoint creates the custom fields used by VM synchronization metadata.

## Proxbox Plugin Config Routes

These route handlers exist in `proxbox_api/routes/proxbox/__init__.py` but are not currently mounted in `main.py`:

- `GET /netbox/plugins-config`
- `GET /netbox/default-settings`
- `GET /settings`

Mounting them requires including that router in app startup if desired.
