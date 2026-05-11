# Overwrite Flags

`SyncOverwriteFlags` is the per-field gate the netbox-proxbox plugin uses to
control whether a given Proxmox-derived value will replace an existing NetBox
value during reconciliation. It is the API-side counterpart of the plugin's
`overwrite_*` boolean fields and the per-endpoint Settings tab.

## What the flags do

Each flag toggles whether one NetBox key is included in the `patchable_fields`
allowlist passed to `rest_reconcile_async` / `rest_bulk_reconcile_async`. When a
flag is `False`, the corresponding key is dropped from the allowlist, so the
PATCH payload sent to NetBox no longer touches that field. New objects are
always populated with the Proxmox value on first create — flags only gate
updates to existing objects.

`True` (the default) preserves the historical always-overwrite behavior. Any
flag set to `None` on the plugin side falls back to the global default
configured on the `ProxboxPluginSettings` row.

## Schema

The flags live in
[`proxbox_api/schemas/sync.py`](https://github.com/emersonfelipesp/proxbox-api/blob/main/proxbox_api/schemas/sync.py)
as `SyncOverwriteFlags`, a `ProxboxBaseModel`. There are 23 boolean fields
grouped by NetBox resource:

| Group | Flags |
|---|---|
| Device | `overwrite_device_role`, `overwrite_device_type`, `overwrite_device_tags`, `overwrite_device_status`, `overwrite_device_description`, `overwrite_device_custom_fields` |
| Virtual Machine | `overwrite_vm_role`, `overwrite_vm_type`, `overwrite_vm_tags`, `overwrite_vm_description`, `overwrite_vm_custom_fields` |
| Cluster | `overwrite_cluster_tags`, `overwrite_cluster_description`, `overwrite_cluster_custom_fields` |
| Node Interface | `overwrite_node_interface_tags`, `overwrite_node_interface_custom_fields` |
| Storage | `overwrite_storage_tags` |
| VM Interface | `overwrite_vm_interface_tags`, `overwrite_vm_interface_custom_fields` |
| IP Address | `overwrite_ip_status`, `overwrite_ip_tags`, `overwrite_ip_custom_fields`, `overwrite_ip_address_dns_name` |

The flag list, order, and default value (`True`) must stay in lock-step with
`netbox_proxbox.constants.OVERWRITE_FIELDS` on the plugin side.

## How it reaches the routes

Each sync route accepts the schema as a flattened query group:

```python
from proxbox_api.dependencies import ResolvedSyncOverwriteFlagsDep
from proxbox_api.schemas.sync import SyncOverwriteFlags

@router.get("/devices/create/stream")
async def create_devices_stream(
    overwrite_flags: ResolvedSyncOverwriteFlagsDep = SyncOverwriteFlags(),
):
    ...
```

FastAPI flattens the model: the URL `?overwrite_device_tags=false` is
equivalent to constructing `SyncOverwriteFlags(overwrite_device_tags=False)`.
Routes also pass the bound model through the shared
`resolved_sync_overwrite_flags` dependency, which re-reads the raw query string
and makes any canonical flat `overwrite_*` key authoritative. This guards the
plugin/backend contract against FastAPI/Pydantic query-model behavior changes.

For VM sync routes, `overwrite_vm_role`, `overwrite_vm_type`,
`overwrite_vm_tags`, `overwrite_vm_description`, and
`overwrite_vm_custom_fields` are also exposed as explicit flat top-level query
parameters for backward compatibility. When both are present, the flat
parameter wins; when only the flat parameter is omitted
(`None`), the corresponding field on `overwrite_flags` is used. Resolution is
done at the entry of each route via the
`_resolve_vm_overwrites(...)` helper in
`proxbox_api/routes/virtualization/virtual_machines/sync_vm.py`.

The DCIM device sync route accepts the same canonical flat query shape. For
example, `/dcim/devices/create/stream?overwrite_device_role=false` must result
in `role` being omitted from existing-device PATCH payloads.

## How it reaches the reconciler

Service modules (e.g. `services/sync/storages.py`,
`services/sync/network.py`, `services/sync/device_ensure.py`,
`services/sync/devices.py`, `services/sync/bridge_interfaces.py`) accept
`overwrite_flags: SyncOverwriteFlags | None = None`. Each service builds its
own `patchable_fields` set — typically all "scalar" identity fields
unconditionally, plus optional keys gated on the per-resource flag — and
passes it into the reconciler:

```python
patchable: set[str] = {"name", "virtual_machine", "enabled", ...}
if overwrite_flags is None or overwrite_flags.overwrite_vm_interface_tags:
    patchable.add("tags")
if overwrite_flags is None or overwrite_flags.overwrite_vm_interface_custom_fields:
    patchable.add("custom_fields")

await rest_bulk_reconcile_async(
    nb,
    "/api/virtualization/interfaces/",
    payloads=interface_payloads,
    patchable_fields=frozenset(patchable),
    ...,
)
```

Setting `overwrite_flags=None` (or omitting it) keeps every key patchable,
which preserves the historical always-overwrite semantics required by
older callers.

## Where the device flags are enforced

The `overwrite_device_role`, `overwrite_device_type`, and
`overwrite_device_tags` flags are honored on **two** distinct write paths,
because a single Proxmox-to-NetBox sync can touch a parent `Device` record
from either side:

- **Bulk DCIM path** — `ensure_proxmox_devices_bulk()` in
  `services/sync/device_ensure.py` runs during a full cluster/node sync.
- **Per-VM path** — `_ensure_device()` in the same module runs during a
  single VM sync (and during VM sync streaming) to materialize the VM's
  parent `Device` if it is not already in NetBox.

Both paths build their `patchable_fields` set through the shared helper
`_compute_device_patchable_fields(...)`, which is the single source of
truth for the device allowlist. This guarantees that flipping
`overwrite_device_type=False` survives **every** sync mode — issue #342
was a regression from the per-VM path bypassing the allowlist and
silently reverting `device_type` to `Proxmox Generic Device`.

## Plugin contract

The plugin and the API rely on the same flag names being canonical on both
ends:

- Plugin: `netbox_proxbox/constants.py::OVERWRITE_FIELDS` (single source of
  truth on the plugin side)
- API: `proxbox_api/schemas/sync.py::SyncOverwriteFlags.model_fields`

Adding, removing, or reordering flags must be done on both repos in the same
release. The cross-repo `tests/test_overwrite_flags_contract.py` (in both
projects) compares each side against a committed JSON manifest and fails CI on
drift.

## Inheritance and resolution

The plugin resolves the per-endpoint values by combining the global plugin
settings with the per-`ProxmoxEndpoint` overrides. The per-endpoint table uses
`NullBooleanField`s so each row is tri-state:

- `True` — override: always overwrite for this endpoint
- `False` — override: never overwrite for this endpoint
- `None` — inherit from the global setting

The plugin's `effective_overwrites_for_endpoint(...)` flattens the resolved
booleans into the SSE query string forwarded to proxbox-api, where they
materialize as `SyncOverwriteFlags` query parameters.

## Related

- Plugin docs: `docs/configuration/sync-overwrite-flags.md` (in the
  netbox-proxbox repo) covers the UI, the Settings tab, and the inheritance
  model.
- API tests: `tests/test_sync_overwrite_flags.py` and
  `tests/test_patchable_fields.py` lock in the schema contract and the
  per-service propagation.
