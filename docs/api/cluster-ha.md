# Proxmox Cluster HA Reference

Read-only Proxmox High-Availability endpoints aggregated across every configured cluster. Power the **HA tab** on the NetBox VM detail page and the **cluster-wide HA Status** page added by `netbox-proxbox` v0.0.15+ for [issue #243](https://github.com/emersonfelipesp/netbox-proxbox/issues/243).

- **Available since:** proxbox-api `v0.0.11`. The matching consumer floor is `netbox-proxbox >= 0.0.15` — older plugin builds do not call these paths.
- **Mutations are intentionally out of scope.** Adding/removing a resource, migrating, relocating, or any HA group CRUD is not exposed here and may land in a follow-up release.
- **Source paths on Proxmox:** `/cluster/ha/status/current`, `/cluster/ha/resources`, `/cluster/ha/groups`. The router merges results across every `ProxmoxSession` configured in the backend, so a single request fans out one call per cluster.

## Endpoint Summary

| Method | Path | Returns | Notes |
|--------|------|---------|-------|
| `GET`  | `/proxmox/cluster/ha/status` | `list[HaStatusItemSchema]` | Per-service CRM/LRM rows plus quorum/master entries from `/cluster/ha/status/current`. Errors fetching one cluster surface as a single row with `status="error: ..."` and the rest of the clusters still aggregate. |
| `GET`  | `/proxmox/cluster/ha/resources` | `list[HaResourceSchema]` | Configured HA resources merged with their live runtime state (node, CRM state, request state). Errors fetching one cluster log via `logger.exception` and that cluster contributes zero rows. |
| `GET`  | `/proxmox/cluster/ha/resources/by-vm/{vmid}` | `HaResourceSchema \| null` | Convenience lookup for a single VM/CT id. Tries `vm:{vmid}` first, falls back to `ct:{vmid}`. Returns `null` (not 404) when the guest is not HA-managed so the NetBox tab can render an empty state. |
| `GET`  | `/proxmox/cluster/ha/groups` | `list[HaGroupSchema]` | List of HA groups across clusters with merged detail (nodes, restricted, nofailback). |
| `GET`  | `/proxmox/cluster/ha/groups/{group}` | `HaGroupSchema \| null` | Single HA group detail across clusters; returns `null` when no cluster has the group. |
| `GET`  | `/proxmox/cluster/ha/summary` | `HaSummarySchema` | Composed `{status, groups, resources}` envelope. Calls the three handlers concurrently via `asyncio.gather` so the cluster-wide page only triggers one round-trip per render. |

All endpoints require the `X-Proxbox-API-Key` header like the rest of `proxbox-api`. They reuse `ProxmoxSessionsDep` and inherit the global rate limit.

## Response Schemas

Defined in `proxbox_api/routes/proxmox/ha.py` and exported as Pydantic v2 models. Every field is optional because Proxmox mixes service rows with cluster-wide rows (quorum, master, lrm:&lt;node&gt;) and service rows in different cluster states omit different keys.

### `HaStatusItemSchema`

```jsonc
{
  "cluster_name": "lab",      // injected by proxbox-api so multi-cluster rows stay disambiguated
  "id": null,                  // raw id key from Proxmox, when present
  "type": "service",           // "service" | "quorum" | "master" | "lrm" | ...
  "sid": "vm:100",             // service id; null on quorum/master rows
  "node": "pve01",
  "state": "started",
  "status": "started",
  "crm_state": "started",
  "request_state": "started",
  "quorate": true,
  "failback": null,
  "max_relocate": 1,
  "max_restart": 1,
  "timestamp": 1730000000
}
```

### `HaResourceSchema`

```jsonc
{
  "cluster_name": "lab",
  "sid": "vm:100",
  "type": "vm",                // "vm" | "ct"
  "state": "started",
  "group": "ha-group-a",
  "max_relocate": 2,
  "max_restart": 1,
  "failback": true,
  "comment": null,
  "digest": "abc",
  // Live runtime state merged from /cluster/ha/status/current when present.
  "node": "pve02",
  "crm_state": "started",
  "request_state": "started",
  "status": "started"
}
```

### `HaGroupSchema`

```jsonc
{
  "cluster_name": "lab",
  "group": "ha-group-a",
  "type": "group",
  "nodes": "pve01:1,pve02:2",
  "restricted": true,
  "nofailback": false,
  "comment": null,
  "digest": null
}
```

### `HaSummarySchema`

```jsonc
{
  "status":    [/* HaStatusItemSchema, ... */],
  "groups":    [/* HaGroupSchema, ... */],
  "resources": [/* HaResourceSchema, ... */]
}
```

## Error Handling

- The router uses `logger.exception` (not silent `except`) when a per-cluster fetch fails so dashboards on the netbox-proxbox side never see "silent zeros".
- A failed `/cluster/ha/status` fetch records one synthetic `HaStatusItemSchema` row for that cluster with `status="error: <message>"`; healthy clusters still contribute their rows.
- A failed `/cluster/ha/resources` or `/cluster/ha/groups` top-level fetch logs and skips that cluster.
- Inner-loop sub-detail fetches (single SID resource detail, single group detail) log at `debug` level and fall back to the list-row payload — the response is best-effort, never a 5xx.
- `/proxmox/cluster/ha/resources/by-vm/{vmid}` always returns `null` for unmanaged VM/CT ids; the consumer must treat `null` as "not HA-managed" instead of error.

## Coercion Rules

Proxmox returns `0`/`1` integers, kebab-case keys (`max-restart`, `crm-state`), and string booleans on some endpoints. The router normalizes them through `_coerce_int` and `_coerce_bool`:

- `bool` → `bool` itself; `int`/`float` → `bool(value)`; `"1"`/`"true"`/`"yes"`/`"on"` → `True`; `"0"`/`"false"`/`"no"`/`"off"`/`""` → `False`; otherwise `None`.
- `bool` → `int(value)`; `int` → itself; numeric `str` → `int(str)`; otherwise `None`.

The kebab-case fallbacks (`row.get("max_restart") or row.get("max-restart")`) are intentional — the same reading code handles both pre-8.x and 8.x Proxmox payload styles.

## How `netbox-proxbox` Consumes These Endpoints

`netbox-proxbox` exposes a thin REST shim under `/api/plugins/proxbox/ha/` that proxies into these endpoints:

| Plugin shim                                  | Backend call                                         |
|----------------------------------------------|------------------------------------------------------|
| `GET /api/plugins/proxbox/ha/summary/`       | `GET /proxmox/cluster/ha/summary`                    |
| `GET /api/plugins/proxbox/ha/vm/{vmid}/`     | `GET /proxmox/cluster/ha/resources/by-vm/{vmid}`     |

The plugin renders these as a Django dashboard page (`HAClusterView`) and a per-VM HA tab (`ProxmoxVMHATabView`, gated on the `proxmox_vm_id` custom field). The plugin's own page documents the consumer side — see `netbox-proxbox/docs/api/ha.md`.

## Tests

Unit tests for the router live in `tests/test_proxmox_ha_routes.py`. They patch `get_ha_status_current`, `get_ha_resources`, and `get_ha_groups` from `proxbox_api.services.proxmox_helpers` and exercise:

- `ha_status` aggregation across rows and the synthetic error row when a helper raises.
- `ha_resources` merging runtime state from `/status/current` into list rows.
- `ha_resource_by_vm` returning `null` when no SID is HA-managed and falling back from `vm:{vmid}` to `ct:{vmid}`.
- `ha_groups` list + detail merge and `null` when a group is missing on every cluster.
- `ha_summary` parallel composition.
- Router registration under the `/proxmox/cluster/ha/*` prefix in the live app factory.

Run them with:

```bash
uv run pytest tests/test_proxmox_ha_routes.py -q
```

## See Also

- [HTTP API Reference — High-Availability (read-only)](http-reference.md#high-availability-read-only) for the consolidated route listing alongside the rest of the `/proxmox/*` surface.
- [HTTP API Reference — VM Operational Verbs](http-reference.md#vm-operational-verbs) for the companion write surface (start/stop/snapshot/migrate) that depends on `ProxmoxEndpoint.allow_writes`.
- `netbox-proxbox/docs/api/ha.md` for the plugin-side consumer of these endpoints.
