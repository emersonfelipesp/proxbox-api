# HTTP API Reference

This page summarizes main HTTP endpoints exposed by `proxbox-api`.

For full request/response schemas, use runtime OpenAPI at `/docs`.

## Root and utility

- `GET /` - Service metadata and links.
- `GET /cache` - Inspect in-memory cache snapshot.
- `GET /clear-cache` - Clear in-memory cache.
- `GET /sync-processes` - List sync process records from NetBox plugin API.
- `POST /sync-processes` - Create a sync process record in NetBox plugin API.

## NetBox routes (`/netbox`)

- `POST /netbox/endpoint` - Create singleton NetBox endpoint.
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

## Proxmox routes (`/proxmox`)

### Endpoint configuration CRUD

- `POST /proxmox/endpoints`
- `GET /proxmox/endpoints`
- `GET /proxmox/endpoints/{endpoint_id}`
- `PUT /proxmox/endpoints/{endpoint_id}`
- `DELETE /proxmox/endpoints/{endpoint_id}`

Validation rules:

- Require `password` or (`token_name` and `token_value`).
- `token_name` and `token_value` must be set together.
- Endpoint names must be unique.

### Session and discovery

- `GET /proxmox/sessions`
- `GET /proxmox/version`
- `GET /proxmox/`
- `GET /proxmox/storage`
- `GET /proxmox/nodes/{node}/storage/{storage}/content`
- `GET /proxmox/{top_level}`
- `GET /proxmox/{node}/{type}/{vmid}/config`

### Cluster and node data

- `GET /proxmox/cluster/status`
- `GET /proxmox/cluster/resources`
- `GET /proxmox/nodes/`
- `GET /proxmox/nodes/{node}/network`
- `GET /proxmox/nodes/{node}/qemu`

### Viewer code generation

- `POST /proxmox/viewer/generate`
- `GET /proxmox/viewer/openapi`
- `GET /proxmox/viewer/openapi/embedded`
- `GET /proxmox/viewer/integration/contracts`
- `GET /proxmox/viewer/pydantic`
- `POST /proxmox/viewer/routes/refresh`

### Runtime-generated live proxy routes

`proxbox-api` now mounts runtime-generated Proxmox proxy routes from the embedded generated
OpenAPI contract under:

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
- Generated response models now cover object, array, scalar, and `null` response schemas rather than object-shaped responses only.
- For array responses whose items are objects, generation now emits `{Operation}ResponseItem` plus `RootModel[list[{Operation}ResponseItem]]`, so Swagger shows concrete item fields instead of generic `additionalProp` placeholders.
- Generated routes appear in FastAPI `/docs` and `/openapi.json`.
- `latest` routes are mounted before older version tags so they appear first in Swagger.
- Generated routes are prioritized ahead of older handcrafted `/proxmox/*` routes so path collisions resolve to the generated API surface.

Path parameter normalization:

- When the Proxmox viewer uses path parameter names that are not valid FastAPI identifiers, the mounted FastAPI route uses a normalized placeholder name.
- Example:
  - Proxmox contract path: `/nodes/{node}/hardware/pci/{pci-id-or-mapping}`
  - Mounted FastAPI path: `/proxmox/api2/latest/nodes/{node}/hardware/pci/{pci_id_or_mapping}`
- The upstream proxmoxer call still uses the original Proxmox parameter name from the generated OpenAPI contract.

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

- Handcrafted sync-facing routes still call proxmoxer directly, but now do so through `proxbox_api/services/proxmox_helpers.py`.
- That helper layer validates live proxmoxer payloads with the generated models in `proxbox_api/generated/proxmox/latest/pydantic_models.py` before returning data to route handlers.
- This avoids internal HTTP round-trips while keeping VM config, cluster status, cluster resources, storage listing, and node storage content aligned with the generated contract used by `/proxmox/api2/*`.

Examples of generated route shapes:

- `GET /proxmox/api2/latest/cluster/resources`
- `GET /proxmox/api2/8.3.0/nodes/{node}/qemu/{vmid}/config`
- `POST /proxmox/api2/latest/access/acl`
- `GET /proxmox/api2/cluster/resources` as the compatibility alias for `latest`

Refresh response shape:

- `mounted_versions`: the versioned route sets currently mounted in FastAPI.
- `alias_version_tag`: the version used by `/proxmox/api2/*`.
- `cache_path`: persisted manifest path used to preserve generated routes across reloads.
- `cache_source`: whether the last registration used the persisted runtime cache or scanned generated artifacts.
- `versions.<tag>.path_count`: number of OpenAPI paths mounted for that version.
- `versions.<tag>.method_count`: number of `GET`/`POST`/`PUT`/`DELETE` operations mounted for that version.
- `versions.<tag>.schema_version`: the `info.version` value from the generated OpenAPI document.

Test coverage:

- `tests/test_generated_proxmox_routes.py` runs a mock-based exhaustive route suite over every generated operation for every available version plus the `latest` alias.
- Each generated operation is exercised individually with schema-generated path/query/body inputs and a schema-generated mock upstream response.
- `tests/test_pydantic_generator_models.py` verifies generated response models for array, scalar, `null`, and aliased object payloads.
- `tests/test_session_and_helpers.py` verifies the typed proxmox helper layer and confirms the handcrafted sync dependencies return helper-validated payloads.

## DCIM routes (`/dcim`)

- `GET /dcim/devices`
- `GET /dcim/devices/create` - create NetBox devices from Proxmox nodes (returns JSON on completion).
- `GET /dcim/devices/create/stream` - SSE streaming variant. Emits per-device `step` events with granular progress while devices are being created.
- `GET /dcim/devices/{node}/interfaces/create`
- `GET /dcim/devices/interfaces/create`

## Virtualization routes (`/virtualization`)

- `GET /virtualization/cluster-types/create` (placeholder)
- `GET /virtualization/clusters/create` (placeholder)
- `GET /virtualization/virtual-machines/create` - create NetBox VMs from Proxmox resources (returns JSON on completion).
- `GET /virtualization/virtual-machines/create/stream` - SSE streaming variant. Emits per-VM `step` events with granular progress while VMs are being created.
- `GET /virtualization/virtual-machines/`
- `GET /virtualization/virtual-machines/{id}`
- `GET /virtualization/virtual-machines/summary/example`
- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`

Additional test/helper and TODO endpoints also exist in this route group.

## Full update

- `GET /full-update` - runs device sync then VM sync, returns combined JSON result.
- `GET /full-update/stream` - SSE streaming variant. Emits per-object `step` events for both devices and VMs during the full synchronization.

## SSE streaming format

All `/stream` endpoints return `Content-Type: text/event-stream` and emit three event types:

| Event    | Description |
|----------|-------------|
| `step`   | Progress frame. Contains `step` (object kind, e.g. `device`, `virtual_machine`), `status` (`started`, `progress`, `completed`, `failed`), `message` (human-readable), `rowid` (object name/ID), and `payload` (original websocket-style JSON). |
| `error`  | Error frame. Contains `step`, `status: "failed"`, `error`, and `detail`. |
| `complete` | Final frame. Contains `ok` (boolean), `message`, and optionally `result` or `errors`. |

Example `step` event for a device:

```
event: step
data: {"step":"device","status":"progress","message":"Processing device pve01","rowid":"pve01","payload":{"object":"device","type":"create","data":{"rowid":"pve01","completed":false}}}
```

Headers:

- `Cache-Control: no-cache`
- `X-Accel-Buffering: no`

## Extras routes (`/extras`)

- `GET /extras/extras/custom-fields/create`

This endpoint creates expected custom fields used by VM synchronization metadata.

## Proxbox plugin config routes

These route handlers exist in `proxbox_api/routes/proxbox/__init__.py` but are not currently mounted in `main.py`:

- `GET /netbox/plugins-config`
- `GET /netbox/default-settings`
- `GET /settings`

Mounting them requires including that router in app startup if desired.
