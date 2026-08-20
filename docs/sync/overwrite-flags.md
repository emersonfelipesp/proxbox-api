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

### VM role snapshot lock

VM roles use a durable ownership snapshot in addition to the ordinary
allowlist. `ProxboxVirtualMachineSyncState.proxmox_last_synced_role_id` records
the DeviceRole ID written by the last successful sync:

- current role differs from snapshot + `overwrite_vm_role=false`: preserve the
  operator-edited role and keep the old snapshot;
- current role equals snapshot: the role remains sync-managed and may roll
  forward when the configured default changes;
- existing role with no snapshot: preserve it and record it as the initial
  snapshot, so upgrades fail safe;
- unavailable, failed, or conflicting snapshot read: preserve the current role
  without writing a snapshot, because absence was not verified;
- `overwrite_vm_role=true`: release a proven operator lock and write the
  current desired role plus its new snapshot.

The snapshot is written only after successful reconciliation and is retried as
a required ownership write. After an exhausted response, the backend re-reads
the typed snapshot authoritatively: it accepts a confirmed commit, or restores
and verifies both the previous role and previous snapshot before marking that
VM failed. The pair stays aligned, so the next pass does not mistake response
loss for an operator edit.
Full/bulk, individual, and sidecar-adoption paths share this truth table.
Full/bulk applies it after the Python/Rust queue seam, so the selected
reconciliation engine cannot bypass the policy.

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

## Behavior flags (`SyncBehaviorFlags`)

Separate from the per-field `overwrite_*` gates, `SyncBehaviorFlags` carries
opt-in behavior toggles that compose independently:

- `parse_description_metadata` — parse a fenced `netbox-metadata` block from each
  Proxmox object's description and apply its NetBox primary-key overrides. It governs
  **only** the overrides. It does **not** control whether the Proxmox note reaches
  NetBox — see [VM description and comments](#vm-description-and-comments) below.
- `sync_vm_platform_from_guest_agent` — default `false`. When true, the VM sync asks the
  QEMU guest agent for `get-osinfo` and uses the reported product as the NetBox
  platform. See [VM platform from the guest OS](#vm-platform-from-the-guest-os) below.
- `custom_fields_enabled` — **deprecated legacy custom fields**, default `false`.
  When `false`, the typed `Proxbox*SyncState` sidecars are the sole source of
  truth and no legacy reflection custom fields are written, read, or reconciled.
  It composes with the `overwrite_*_custom_fields` gates: a custom-field value is
  only written to NetBox when the relevant `overwrite_*_custom_fields` flag **and**
  `custom_fields_enabled` are both true, while sidecar writes continue whenever
  the `overwrite_*` flag is true. The setting normally comes from the
  `ProxboxPluginSettings.custom_fields_enabled` plugin field; an explicit
  per-request behavior flag overrides it. When enabled, every custom-field path
  emits a deprecation warning.

## VM description and comments

A note kept in a Proxmox VM's `description` field is operator-authored content, so the
sync preserves it rather than overwriting it:

| Proxmox note | NetBox `description` | NetBox `comments` |
|---|---|---|
| absent, blank, or only a `netbox-metadata` fence | `Synced from Proxmox node {node}` | not written |
| one line, ≤ 200 characters | the note | not written |
| multiple lines | the first non-empty line | the complete note |
| first line > 200 characters | first line truncated to 200 with a `…` marker | the complete note |

Rules that are easy to get wrong:

- **This is independent of `parse_description_metadata`.** That flag is about
  `netbox-metadata` PK overrides. Preservation used to be coupled to it, which meant an
  operator had to enable an unrelated override feature to keep their own notes — and even
  then it worked on only one of the three VM payload builders.
- **`netbox-metadata` fences are stripped unconditionally**, with the flag on or off.
  Before the note was written at all, an unstripped fence leaked nowhere; now that the
  note reaches NetBox, leaving it in would put raw JSON into the rendered UI.
- **`comments` is written only when it carries more than `description` does** — multiple
  lines, or a first line that had to be truncated. A one-line note is not duplicated
  across two fields.
- **Proxbox writes `comments` but never clears it.** When there is nothing to write the
  field is omitted from the payload rather than set to an empty string. Clearing it would
  wipe comments an operator wrote by hand on a VM that never had a Proxmox note. The
  trade-off: shortening a multi-line Proxmox note to a single line leaves the previous
  full note in `comments` until it is edited in NetBox.
- **`overwrite_vm_description` gates both fields.** With it `false`, neither
  `description` nor `comments` is patched on an existing VM; both are still set when the
  VM is created. `comments` deliberately reuses this flag rather than adding a new one:
  it is the same operator-authored content under the same consent, and reusing the gate
  means no plugin-side change is required.

All three VM payload builders — the bulk `virtual-machines` stage, the per-VM sync path,
and the VM-create service — share one derivation helper
(`proxmox_to_netbox/description_metadata.py::derive_description_and_comments`). They must
keep sharing it: three private copies of this rule is exactly how they came to behave
three different ways.

## VM platform from the guest OS

NetBox virtual machines have a `platform` field, which is the natural home for the guest
operating system. The sync populates it from data Proxmox already exposes.

### Two tiers

| Tier | Source | Cost | Default |
|---|---|---|---|
| 1 | `ostype` from the VM configuration | none — already in the config payload | always on |
| 2 | QEMU guest agent `get-osinfo` | one extra Proxmox request per eligible VM | **opt-in** |

Tier 1 is coarse (`l26` → `Linux (kernel 2.6 or newer)`) but always available. An unknown
or absent `ostype` leaves the platform **unset** rather than guessing — a wrong operating
system on an inventory page is worse than a blank field.

Tier 2 refines it to the real product and is enabled with
`sync_vm_platform_from_guest_agent`. Because it costs a request per VM, it is gated
twice: by the flag, and by whether the guest can plausibly answer at all. The request is
attempted only for a VM that is **QEMU**, **running**, and **already known to have the
agent enabled** — all three of which the sync knows without asking, so no request is
wasted. Any failure, timeout, or malformed response falls back to the tier-1 value; it
never fails the VM's sync and never fails the stage. The request is bounded by the same
`guest_agent_timeout` setting (env `PROXBOX_GUEST_AGENT_TIMEOUT`, default 15s) the
network-interface agent call uses, so a wedged agent cannot stall the run.

### Naming

The refined name is the agent's `name` plus `version-id`, e.g. `Ubuntu 22.04`.

**Never `pretty-name`.** That field embeds the patch level (`Ubuntu 22.04.5 LTS`), so
every minor update would mint a new NetBox platform and the list would grow without
bound. Platform records are **created only, never rewritten**: an existing platform is
referenced as-is, so a record an operator named, described, or tagged themselves is never
overwritten by a sync that merely needs it to exist. They are matched and created by slug,
so repeated syncs converge on one record, and the platform is resolved **once per run per operating system** rather than
once per VM — an estate's machines share a handful of operating systems, so a per-VM
lookup would be pure request amplification. Unmapped guests are cached as such too, and a
transient NetBox failure is deliberately *not* cached, so it cannot blank the platform for
every remaining VM in the run.

### Overwriting

The platform is set **when a VM is created** and is never patched afterwards. An
operator may well have assigned a platform by hand, and taking that over on the first
sync after upgrading would be a regression dressed as a feature.

There is deliberately **no `overwrite_vm_platform` flag**. The `overwrite_*` set is a
CI-enforced cross-repo contract (`contracts/overwrite_flags.json`, mirrored in
netbox-proxbox alongside `constants.OVERWRITE_FIELDS`), and adding a flag requires
changing both repos in the same release plus the plugin's settings model and per-endpoint
override column. That is out of scope for populating the field; making the behaviour
operator-tunable is a separate, properly cross-repo change.

So with no configuration at all, a deployment sees the platform populated on **newly
created VMs only**, and existing platforms are left exactly as they are.

A `netbox-metadata` fence may pin `platform` per VM, since it is an integer foreign key.
That value lands on creation like any other, and is subject to the same create-only rule —
the fence does not become a back-door overwrite gate for existing VMs.

Platform **records** are likewise created but never rewritten: an existing platform is
referenced as-is, so a record an operator named, described, or tagged themselves is never
overwritten by a sync that merely needs it to exist.

## Related

- Plugin docs: `docs/configuration/sync-overwrite-flags.md` (in the
  netbox-proxbox repo) covers the UI, the Settings tab, and the inheritance
  model.
- API tests: `tests/test_sync_overwrite_flags.py` and
  `tests/test_patchable_fields.py` lock in the schema contract and the
  per-service propagation.
