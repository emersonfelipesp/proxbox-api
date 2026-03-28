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

- `/proxmox/api2/{version_tag}/json/*`
- `/proxmox/api2/json/*` as a compatibility alias to `latest`

Behavior:

- Routes are built at startup for every generated version present under `proxbox_api/generated/proxmox/`.
- Routes are rebuilt on demand with `POST /proxmox/viewer/routes/refresh`.
- The unversioned `/proxmox/api2/json/*` alias forwards to the `latest` generated contract.
- Request bodies and responses are validated with runtime-generated Pydantic models.
- Generated routes appear in FastAPI `/docs` and `/openapi.json`.

Target selection:

- If exactly one Proxmox endpoint exists, generated routes use it automatically.
- If more than one endpoint exists, pass one of:
  - `target_name`
  - `target_domain`
  - `target_ip_address`
- `source` selects whether endpoints come from the local database or NetBox plugin records.

Examples of generated route shapes:

- `GET /proxmox/api2/latest/json/cluster/resources`
- `GET /proxmox/api2/8.3.0/json/nodes/{node}/qemu/{vmid}/config`
- `POST /proxmox/api2/latest/json/access/acl`
- `GET /proxmox/api2/json/cluster/resources` as the compatibility alias for `latest`

## DCIM routes (`/dcim`)

- `GET /dcim/devices`
- `GET /dcim/devices/create`
- `GET /dcim/devices/{node}/interfaces/create`
- `GET /dcim/devices/interfaces/create`

## Virtualization routes (`/virtualization`)

- `GET /virtualization/cluster-types/create` (placeholder)
- `GET /virtualization/clusters/create` (placeholder)
- `GET /virtualization/virtual-machines/create`
- `GET /virtualization/virtual-machines/`
- `GET /virtualization/virtual-machines/{id}`
- `GET /virtualization/virtual-machines/summary/example`
- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`

Additional test/helper and TODO endpoints also exist in this route group.

## Extras routes (`/extras`)

- `GET /extras/extras/custom-fields/create`

This endpoint creates expected custom fields used by VM synchronization metadata.

## Proxbox plugin config routes

These route handlers exist in `proxbox_api/routes/proxbox/__init__.py` but are not currently mounted in `main.py`:

- `GET /netbox/plugins-config`
- `GET /netbox/default-settings`
- `GET /settings`

Mounting them requires including that router in app startup if desired.
