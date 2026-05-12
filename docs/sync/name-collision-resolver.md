# VM Name Collision Resolver

NetBox's `virtualization.VirtualMachine` model enforces uniqueness on
`(cluster, tenant, name)` with `nulls_distinct=False`. When proxbox-api
synchronizes two Proxmox VMs that share a `name` and land in the same NetBox
cluster, NetBox refuses the second `POST` with a `400` validation error.

The name-collision resolver assigns deterministic ``" (N)"`` suffixes so both
records can coexist, and detects operator renames so a manually-edited NetBox
record is not silently overwritten on the next sync.

## Where the resolver lives

`proxbox_api/services/name_collision.py`

- `NameResolution` — frozen dataclass returned by the resolver.
- `_pick_suffix(candidate, used)` — pure helper that picks the smallest free
  ``" (N)"`` suffix (or returns the bare name when no collision exists).
- `resolve_unique_vm_name(...)` — async entrypoint used by both the bulk
  sync path (`sync_vm.py`) and the individual sync path
  (`services/sync/individual/vm_sync.py`).

Name matching is case-insensitive via `str.casefold`. The returned
`resolved_name` preserves the candidate's original casing for display.

## When the resolver runs

### Bulk sync (`/full-update`)

In `_run_full_update_vm_batch`, immediately after the NetBox snapshot is
loaded and before the operation queue is built. The pre-pass:

1. Groups prepared VMs by NetBox cluster id.
2. Within each cluster, sorts VMs by `(proxmox_cluster_name.casefold(),
   proxmox_vmid)` so the lower-VMID VM keeps the bare name across re-runs.
3. Builds `used_names_in_cluster` from the snapshot, filtered to remove the
   record currently owned by each VMID (so the same VM can keep its name on
   re-sync without colliding with itself).
4. Calls `resolve_unique_vm_name(...)` per VM and mutates
   `prepared.desired_payload["name"]` when a suffix or operator rename is
   applied.
5. Emits `WebSocketSSEBridge.emit_duplicate_name_resolved(...)` for each
   renamed VM.

### Individual sync (`/sync/virtual-machines/{vmid}`)

`sync_vm_individual` issues one `GET
/api/virtualization/virtual-machines/?cluster_id=<id>&limit=0`, builds a
fresh `used_names_in_cluster` set and `existing_vm_by_vmid` map, calls
`resolve_unique_vm_name(...)`, and patches the payload's `name` before the
reconcile.

## Operator-rename detection

If NetBox has a `VirtualMachine` whose `custom_fields.proxmox_vm_id` matches
the incoming VMID **and** whose current `name` is neither the bare candidate
nor any algorithmic suffix of it (`gateway (2)`, `gateway (3)`, …), the
resolver returns the operator's name with `operator_renamed=True`. The
caller emits a `duplicate_name_resolved` warning frame with
`operator_renamed: true` and skips the rename.

Algorithmic-looking operator names (e.g. an operator who manually typed
`gateway (2)`) cannot be distinguished from the resolver's own output and
will be retained on subsequent syncs as if they were resolver-assigned.

## Boundaries

- **Per NetBox cluster.** Two VMs in different NetBox clusters do not
  collide structurally and are left as bare names — even if their Proxmox
  cluster labels differ. This matches NetBox's structural uniqueness.
- **Stable identifier.** `custom_fields.proxmox_vm_id` is the durable
  cross-reference. The resolver may flip which VM keeps the bare name if
  Proxmox cluster names are re-ordered, but the VMID-keyed link survives.
- **Legacy records.** A NetBox VM with no `proxmox_vm_id` custom field
  cannot be matched by VMID; the resolver treats it as a non-Proxbox record
  and will not consider it an operator rename.

## SSE frame shape

```json
{
  "event": "duplicate_name_resolved",
  "cluster": "cluster-a",
  "original_name": "gateway",
  "resolved_name": "gateway (2)",
  "vmid": 101,
  "suffix_index": 2,
  "operator_renamed": false
}
```

Schema authority:

- `proxbox_api.schemas.stream_messages.DuplicateNameResolvedMessage`
- builder: `build_duplicate_name_resolved_message`
- emitter: `WebSocketSSEBridge.emit_duplicate_name_resolved`

The netbox-proxbox mirror is
`netbox_proxbox.schemas.backend_proxy.SseDuplicateNameResolvedPayload`, and
`contracts/proxbox_api_sse_schema.json` is pinned by
`tests/test_sse_schema_mirror.py`.

## Worked example

Two Proxmox VMs named `gateway`, vmids `101` and `205`, both syncing into
NetBox cluster `5`:

| Pass | Order processed | Result |
|---|---|---|
| 1   | 101 first       | `gateway`     (suffix 1, no frame)        |
| 1   | 205 second      | `gateway (2)` (suffix 2, frame emitted)   |
| 2   | 101 first       | `gateway`     (idempotent, no frame)      |
| 2   | 205 second      | `gateway (2)` (idempotent, frame emitted) |

If an operator then renames the NetBox record for vmid `101` to
`gateway-prod-a`, the next sync emits a frame with
`operator_renamed=true, resolved_name="gateway-prod-a"` and leaves the
record alone.
