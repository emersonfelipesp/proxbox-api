# proxbox_api/services/sync Directory Guide

## Workspace Context

This file lives at `<repository-root>/proxbox_api/services/sync/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Synchronization services responsible for NetBox object creation from Proxmox data.

## Current Modules

- `__init__.py`: sync service namespace for Proxmox-to-NetBox flows.
- `cluster_links.py`: repairs netbox-proxbox `ProxmoxCluster.netbox_cluster`
  links by exact NetBox cluster-name resolution after cluster reconciliation.
- `clusters.py`: cluster synchronization helpers.
- `device_ensure.py`: device creation and reconciliation helpers. When several
  identity sidecars claim different devices for one node, deleted devices are
  ignored, the target cluster decides ownership (a site match counts only for
  clusterless devices), and anything still ambiguous fails closed. Device names
  are never evidence.
- `guest_vm_interface.py`: best-effort netbox-proxbox plugin reconciliation for
  guest OS VM interfaces and guest-interface-to-core-IP links.
- `devices.py`: device synchronization from Proxmox nodes to NetBox.
- `network.py`: network and interface sync helpers.
- `reconciliation/`: pure operation-queue reconciliation, Python fallback,
  optional Rust bridge, mismatch metric, and shared VM operation types.
- `sdn.py`: read-only Proxmox SDN inventory sync, NetBox L2VPN/Prefix/plugin
  metadata reconciliation, and optional `netbox_bgp` projection controlled by
  `sync_mode_sdn_bgp`.
- `snapshots.py`: snapshot sync helpers.
- `storage_links.py`: storage-to-NetBox relationship helpers.
- `storages.py`: storage sync helpers.
- `task_history.py`: node-oriented Proxmox archive pagination and one-pass
  NetBox task-history reconciliation. It loads VM sync-state sidecars once and
  uses their endpoint + cluster + VMID + type identity authoritatively, with
  fail-closed malformed/duplicate rows and opt-in legacy fallback only for
  absent rows. A successful full scan skips unmanaged NetBox VMs, but selected
  VMs without identity remain fatal: task history has no lenient selection mode
  (see `vm_filter.py`). It also provides UPID conflict detection,
  requested-scope coverage checks, a run-global fetch semaphore, archive
  terminal status (no per-UPID status N+1), and degraded results for
  partial/no-progress collection plus fatal exceptions for unusable identity,
  total coverage loss, incomplete explicit VM lookup coverage, NetBox
  pagination, or global reconcile failures.
- `virtual_disks.py`: VM disk sync helpers.
- `virtual_machines.py`: virtual machine payload and sync helpers.
- `vm_cluster_guard.py`: cross-cluster guard shared by every endpoint/vmid-keyed
  NetBox VM lookup (`vm_record_in_cluster`, `filter_vm_records_in_cluster`,
  `log_cross_cluster_rejection`).
- `vm_coordinator.py`: VM sync orchestration.
- `vm_create.py`: VM create path helpers.
- `stage_result.py`: result carriers for stages that finish degraded
  (`WarningList`, `attach_skips_to_dict`, `attach_skips_to_list`,
  `response_with_stage_warnings`, `degraded_suffix`).
- `vm_filter.py`: VM ownership filtering and sidecar identity hydration under an
  explicit `SelectionMode` (see "Strict vs lenient VM selection" below).
- `vm_helpers.py`: shared VM helper functions, including `to_mapping()` (coerces
  a NetBox record-ish value to a dict: plain dicts, netbox-sdk `Record`
  `serialize()`, Pydantic v2 `model_dump()`, Pydantic v1 `dict()`, and
  `RootModel.root`), `record_id()` (extracts
  a NetBox record id from dict/serialized/object values) and
  `resolve_netbox_cluster_id_by_name()` (read-only cluster-id lookup by name,
  with optional caching; returns `None` when the cluster does not exist). These
  back the `(cluster_id, vmid)` scoping that keeps same-`vmid` VMs on different
  clusters from being conflated (issue #223). It also owns
  `chunk_netbox_multi_value_ids()`: stable positive-ID deduplication into groups
  of at most 100 for NetBox repeated multi-value query parameters.
- `vm_network.py`: VM network sync helpers.
- `vm_network_processor.py`: VM network parsing and processing helpers.
- `vmid_helpers.py`: VMID lookup and coordination helpers.
- `individual/`: targeted single-object sync workflows.

## How These Services Work

- Route handlers call these helpers to keep HTTP orchestration thin.
- These modules implement idempotent Proxmox-to-NetBox sync flows and journal tracking.
- The VM helpers split orchestration, filtering, network processing, and object creation so the route layer does not need to duplicate state handling.
- `reconciliation/` is the deterministic sync seam: it receives prepared state
  and NetBox snapshots, returns queue operations, and performs no I/O.
- **Task-history request bound.** Never call task-history sync inside a per-VM
  loop. Collect each selected node archive once with `start`/`limit` pagination
  and a fixed `until`, then reconcile all mapped UPIDs in one bulk call with
  individual fallback disabled. Every aggregate scans the NetBox task-history
  table once because the schema uses UPID as its global lookup key; a row
  attached to the wrong VM must remain visible for reassignment. Later-page,
  repeated-page, and no-new-UPID failures retain earlier rows and return
  `degraded=true`; partially missing target scopes and true ownership conflicts
  do the same, while unrelated archive VMIDs are normal skips. Fatal
  identity/total-coverage/pagination/reconcile failures raise
  `ProxboxException`, and cancellation must propagate before reconciliation.
  Unselected aggregates scope authoritative sidecar validation to the active
  `(endpoint, cluster)` sessions, so retired endpoint-less rows outside those
  clusters are ignored. Active-scope corruption and all explicitly selected VM
  identity gaps remain fail-closed.
- `virtual_disks.py` resolves VM config targets from live Proxmox
  `cluster/resources` VMID/type data before falling back to NetBox VM custom
  fields or `device.name`; this avoids disk sync calls against stale or FQDN
  NetBox node names. Explicit multi-VM selections require complete NetBox
  lookup coverage; empty or partial service-level selections are typed 502
  failures rather than zero-count success.
  A guest that no longer exists in Proxmox (`services/proxmox/config.py::is_guest_not_found_error`:
  "does not exist", or "VM Config not found" with no per-session error) is logged at WARNING,
  reported as "VM not found in Proxmox", and still counted skipped; other config failures stay ERROR.
- **IP ownership invariant (all sync paths).** IP sync must never reassign an
  address that already belongs to a *different* object. The shared helper
  `ip_ownership.py` (`_reconcile_interface_ip`) resolves ownership before
  writing: it reuses an IP already on this interface, adopts an *unassigned*
  IP, or creates a new record scoped to this interface — a foreign-owned
  address is left untouched. It is parameterized by `assigned_object_type` /
  `interface_lookup_field` so it serves both `virtualization.vminterface`
  (VM interfaces) and `dcim.interface` (node interfaces). All write paths use
  this rule: `network.py::_resolve_vm_interface_ips` (per-VM-interface),
  `network.py::sync_node_interface_and_ip` (DCIM node IPs),
  `network.py::bulk_reconcile_vm_interface_ips` (bulk — scoped via
  `base_query` + `lookup_fields=["address", "assigned_object_id"]` so a
  foreign-owned address never suppresses creation), and
  `individual/ip_sync.py::sync_ip_individual`. `vm_network.py`
  (`ensure_ip_assigned_to_vm`) likewise only adopts unassigned
  IPs onto a VM and returns `assigned_to_other_object` instead of stealing an
  address owned elsewhere. This prevents the "VM interface wrongly matched to
  another server's IP" defect; both paths stay idempotent across re-syncs.
- **Node-interface type wire invariant.**
  `network.py::sync_node_interface_and_ip` serializes `NetBoxInterfaceType`
  through `.value` before REST reconciliation. NetBox accepts `bridge`, `lag`,
  `virtual`, and `other` for `dcim.Interface.type` — it has **no** `loopback`
  type, which is why the enum no longer carries that value. A Proxmox loopback
  reports its type as `loopback` and degrades to `other`. Open vSwitch bridges,
  bonds, and internal ports map to `bridge`, `lag`, and `virtual`; `OVSPort`
  is materialized as `other` so its authoritative `ovs_bridge` membership is
  retained. The topology phase consumes Linux
  `bridge_ports`/`bond_slaves` and Open vSwitch
  `ovs_ports`/`ovs_bonds`/`ovs_bridge`. Before changing a legacy row from
  `other`, node sync invalidates its cached list read and checks the live cable
  and `mark_connected` state. An incompatible row keeps its existing type and
  emits an actionable warning instead of aborting the entire node sync. A
  duplicate-create race or blocker added before PATCH triggers one authoritative
  re-read and a preserve-only retry in both the full-network and legacy
  per-interface paths. A duplicate that never becomes visible fails the stage;
  an id-less interface is never reported as synchronized. Phase two owns and
  explicitly clears stale `bridge`, `lag`, `parent`, `mode`, and `tagged_vlans`
  values when Proxmox removes a Linux or OVS relationship. Python enum
  labels such as `netboxinterfacetype.bridge` are invalid API choices, and
  NetBox rejects them with `"... is not a valid choice."`, which surfaces as a
  stage that creates zero interfaces rather than as an obvious type error.
  Passing `.value` explicitly is no longer load-bearing: the desired-state
  normalizers in `proxmox_to_netbox/models.py` unwrap enum members themselves
  (see that package's notes). Keep passing it anyway — it states the intent at
  the call site — but a call site that forgets is now correct rather than broken.
- **Node-interface failures are reported, never swallowed.** In
  `network.py::sync_node_network` a failed VLAN, IP, or MAC write is collected
  as `{"device", "interface", "kind", "reason"}` and the result becomes a
  `WarningList` (clean runs stay a plain list). `create_all_device_interfaces`
  aggregates the per-node warnings; REST wraps them as `{"interfaces", "count",
  "warnings", "degraded"}`, SSE and `full_update` surface `warnings` plus
  `degraded` (phase `node-interfaces`). A VLAN interface whose VLAN
  reconciliation failed is passed to the topology patch as `vlan_failed`, which
  omits `mode` and `tagged_vlans` so the existing NetBox assignment is preserved
  rather than cleared; "no VLAN configured" is different and still clears.
- **Cluster/site placement invariant.** After cluster reconciliation, dependent
  device and VM writes use `device_ensure._effective_cluster_site_id()` so a
  cluster's actual `dcim.site` scope wins over a stale endpoint/default site.
  This applies to bulk device sync, full VM sync dependency precompute,
  extracted VM dependency helpers, and individual node/VM sync. Keep new
  cluster-dependent write paths on the same helper to avoid NetBox validation
  errors where the assigned cluster belongs to a different site than the
  dependent object payload.
- **ProxmoxCluster link invariant.** After a NetBox
  `virtualization.Cluster` is reconciled, cluster sync calls
  `cluster_links.sync_proxmox_cluster_netbox_link()` to set or repair every
  matching netbox-proxbox `ProxmoxCluster.netbox_cluster` row by exact cluster
  name. This backfills existing multi-endpoint plugin rows whose
  `netbox_cluster` was previously null and keeps the cloud provisioning
  cluster-to-endpoint map resolvable after re-sync.
- **Shared-MAC guest interfaces.** Guest-agent interfaces that share a Proxmox
  config NIC MAC are aggregated onto the single NetBox VMInterface for that
  config NIC. The merge is keyed by the authoritative config NIC MAC, so VRRP
  virtual MACs and already-normalized Linux alias interfaces are not merged
  across different real NICs.
- **Dual VM interface model.** The default VM interface sync strategy is
  `guest_os_model`: core NetBox `virtualization.VMInterface` rows keep their
  canonical Proxmox config names (`net0`, `net1`, ...), and guest OS names
  (`ens18`, `eth0`, ...) are written to netbox-proxbox plugin
  `GuestVMInterface` rows via `guest_vm_interface.py`. Guest address links must
  reference the existing core `ipam.IPAddress` IDs produced by core interface
  IP reconciliation; guest sync must never POST duplicate IPAM addresses. Plugin
  endpoint 404s from older netbox-proxbox releases are best-effort skips and
  must not fail core sync. Guest plugin reconcile must client-side verify any
  server-filtered first result before patching: `GuestVMInterface` must match
  `(virtual_machine, name)` and address links must match `(guest_interface,
  ip_address)`. If an endpoint ignores ID filters and returns a foreign record,
  skip the guest write rather than patching it. `legacy_rename` is deprecated
  compatibility mode and is the only strategy that may rename core VMInterfaces
  to guest OS names.

- **`to_mapping()` failure is loud, not silent.** Returning `{}` means "this
  record could not be read", and callers go on to read identity and name data
  from it, so an empty result makes a populated record look blank. Every
  give-up path logs (WARNING for an uncoercible type, ERROR for an un-awaited
  coroutine, which is always a caller bug). Do not reintroduce a quiet
  `return {}`; a silent one is what hid netbox-proxbox issue #616 for two
  releases.

- **Proxmox-side VM renames are attributable, not guessed (netbox-proxbox #617).**
  `name_collision.resolve_unique_vm_name()` used to treat *any* difference
  between the stored NetBox name and the incoming Proxmox name as "an operator
  renamed this inside NetBox" and push the stored name back onto the payload —
  so a genuine Proxmox-side rename was silently discarded. The two causes are
  indistinguishable from that function's inputs alone.

  The netbox-proxbox sidecar now carries `proxmox_vm_name`: the name Proxmox
  reported at the last successful sync. That disambiguates them:

  | stored NetBox name | vs `proxmox_vm_name` | meaning | action |
  |---|---|---|---|
  | `web-01` | `== web-01` | untouched in NetBox | Proxmox renamed it → **update** |
  | `gateway-prod` | `!= web-01` | a human edited it | → **preserve** |
  | anything | blank/absent | no evidence | → **preserve** (legacy behaviour) |

  Three rules keep this safe, and all three are load-bearing:

  1. **Blank falls back.** Every row is blank until re-synced after the field
     was added — an entire fleet, immediately after upgrade. Preserving a name
     we are unsure about loses a rename; the opposite destroys an operator's
     deliberate edit, which is worse.
  2. **Write the *Proxmox* name, never the desired payload's.** The resolver may
     have rewritten `desired_payload["name"]` to preserve an operator rename;
     recording that would cement the stale name as "what Proxmox last said" and
     make the field self-confirming. `write_virtual_machine_sync_state` takes
     `proxmox_vm_name` explicitly and callers pass `prepared.resource["name"]`.
  3. **Drop the outgoing name from the used-name set.** Otherwise a VM's own old
     name collides with its new one and forces a spurious `" (2)"` suffix.

  The last-synced names are loaded **once per sync pass** by
  `sync_state_reader.load_vm_last_synced_names()`; the resolver needs one for
  every VM it examines, so a per-VM lookup would be an N+1 across the fleet.
  Coverage: `tests/test_name_collision.py` (both rename directions, the blank
  fallback, self-collision, and a genuine collision still suffixing).

- **VM roles use durable ownership evidence.** The typed VM sidecar field
  `proxmox_last_synced_role_id` records the DeviceRole last written by a
  successful sync. `role_resolution.compute_role_snapshot_decision()` is the
  single truth table: missing evidence captures the current role without
  changing it, a current role that differs from the snapshot is an operator
  edit and is preserved when overwrite is disabled, and a role still matching
  its snapshot may roll forward with a changed managed default. The full/bulk
  path loads all snapshots once and applies the decision in dispatch after the
  Python/Rust queue seam; `vm_create.py`, individual sync, and the network path
  apply the same policy before `rest_reconcile_async`. The writer receives a
  snapshot only after the corresponding reconcile succeeds and persists it
  after the corresponding reconcile succeeds. Unavailable, failed, or
  conflicting reads preserve the role without claiming ownership. Required
  snapshot writes retry three times. After an exhausted response, the shared
  persistence guard authoritatively re-reads typed state, accepts a confirmed
  commit, or restores and verifies both the previous role and snapshot before
  surfacing VM failure. Thus response loss cannot become a false operator lock
  on the next pass. Typed sidecar reads are the only ownership-evidence path.

- **VM platform overwrite is explicit and default-off.**
  `SyncOverwriteFlags.overwrite_vm_platform` is the only path that adds
  `platform` to an existing VM's patchable fields. Omitted flags and the default
  `false` preserve operator-managed NetBox platform assignments; the resolved
  platform remains part of the create payload for new VMs.

## Strict vs lenient VM selection

**What changed.** `vm_filter.py` gained `SelectionMode` (`STRICT` default,
`LENIENT`) on `hydrate_vm_identities_from_sidecars`,
`hydrate_selected_vm_identities`, and `filter_cluster_resources_by_netbox_vm_ids`,
returning `SelectionResult` (a `list` that also carries `.skipped`). The single-VM
`filter_cluster_resources_for_selected_vm` stays strict. The snapshot, virtual-disk,
and backup services take `selection_mode` (default `LENIENT`); the single-VM path
routes pass `STRICT`, and every list/estate route passes `LENIENT` explicitly.

**Source-row alignment invariant.** `filter_cluster_resources_by_netbox_vm_ids`
and `filter_cluster_resources_for_selected_vm` return exactly one row per input
`cluster_resources` row, with the same cluster keys, in the same order. Filtering
only empties resource lists. Stages such as `create_virtual_machines` pair row `i`
with `pxs[i]` and `cluster_status[i]` by position, so dropping a row (for example
when the first source's selected VM is skipped) would bind a later source's VMs to
the wrong Proxmox endpoint. Consumers must tolerate empty rows.

**Why.** Every VM-scoped stage used to raise on the first selected VM whose
sidecar was incomplete or duplicated, or whose owner cluster or live resource
could not be resolved, so one bad or Proxmox-deleted VM aborted the stage and the
whole job for every other VM. Sidecars become incomplete because the VM sync
writes identity only while `overwrite_vm_custom_fields` is on and drops a null
endpoint id; `sync_state_writer.write_virtual_machine_sync_state` now logs a
warning when the live identity is incomplete (persistence is unchanged).

**Downstream effect.** A lenient stage drops the VM with a `WARNING`, finishes for
the others, and reports `[{"netbox_vm_id", "reason"}]` warnings with
`degraded=true` (HTTP 200 / `ok=true`); if every VM is dropped it returns an empty
result with the warnings instead of raising. The netbox-proxbox plugin therefore
sees degraded/warnings for staged runs where it used to see a 502, and
`full_update` includes every stage's warnings (tagged with `phase`) plus
top-level `degraded`. A dropped VM is absent from the backup ownership cache and
never scanned for snapshots, so stale-backup/snapshot cleanup cannot delete its
NetBox rows. A failed/unavailable sidecar scan, an invalid id, and a selection
NetBox does not return in full stay fatal in every mode. Do not add branches to
the noqa-C901 stage orchestrators for this; extend the helpers instead.

**Shared-owner check after hydration.** `hydrate_vm_identities_from_sidecars`
(and so `hydrate_selected_vm_identities`) finishes with
`_reject_shared_hydrated_owners`: two hydrated VMs whose valid sidecars name the
same endpoint raw id, casefolded cluster name, VMID, and VM type are a shared
owner. Detection uses an owner index built from every complete sidecar row of
the scan before it is narrowed to the selection, so a selected VM that shares its
owner with an unselected NetBox VM is also caught (rows with an incomplete
identity are not claimants). Lenient mode warns once per selected claimant and
records it in `.skipped` with a reason naming all claimants (unselected
claimants are not processed or skipped), so snapshot, disk, and backup stages exclude them from writes and
stale cleanup exactly like other lenient drops; strict mode raises before any
stage writes. The same endpoint and VMID in different clusters is not shared.
Every join of a selection to the scan goes through one internal helper,
`_hydrate_scanned_selection`, used by both public hydration functions and by
`_hydrate_selected_sidecar_identities` (so `filter_cluster_resources_for_selected_vm`
and `filter_cluster_resources_by_netbox_vm_ids` also reject a selected VM sharing
its owner with an unselected claimant, before any live-resource matching).
Mirror of `_reject_conflicting_owners`, which covers the owner-matching path.

## Orphan VM sweep

`orphan_sweep.py` is the only owner of end-of-run orphan handling. Its enabled
path preserves the complete tag set, sets `status=decommissioning`, and adds
the `proxbox-soft-deleted` marker; it never hard-deletes a NetBox VM. Dry-run
does not send PATCH requests. `vm_create.py` and the individual VM sync call
`clear_soft_delete_marker()` after successful reconciliation so a reappearing
guest becomes live without losing unrelated operator tags. The marker and
status are the contract consumed by the paired NetBox plugin's human-only
purge page.

The sweep result always carries `skipped_reason` (`None` when it ran). It skips
entirely, without any PATCH or marker-tag creation, when `vm_stage_failed` is set
(a live VM that failed to reconcile was not stamped with the run), when the
sidecar API is unavailable (`sidecar_unavailable`), when the sidecar read failed
(`sidecar_read_failed`), when no in-scope sidecar carries the `run_id`
(`run_not_found`, so a bogus run cannot sweep anything), or when the setting is
off (`disabled`). Right before each PATCH the sweep re-reads the VM's sidecar
(`_no_longer_stale_reason`) and skips a VM that was restamped or changed; NetBox
has no compare-and-set, so this narrows but does not close the race.
`clear_soft_delete_marker(restored_status=...)` also restores the status of a
still-`decommissioning` VM in the same PATCH (used by the bulk VM stage and both
individual paths, `vm_create.py` and `individual/vm_sync.py`, through
`reappeared_vm_status`). `run_orphan_vm_sweep(live_vm_keys=...)` is the
backend-verified guard: `build_live_vm_keys` turns cluster resources into
`(casefolded cluster, vmid, type)` keys and a candidate is marked only if absent
from them (`still_present_in_proxmox` / `identity_incomplete` skips, identity read
from the sidecar by `scan_vm_sidecar_orphan_candidates`);
`live_inventory_unavailable=True` skips the whole sweep. Full-update fetches fresh keys at the sweep boundary via
`_fresh_sweep_inventory` (fail closed to `live_inventory_unavailable`).
`build_live_vm_keys` raises `LiveInventoryError` for an unidentifiable guest row
(type/vmid derived from `id` when missing). Before each PATCH the tag list is rebuilt
from a fresh VM read (`vm_unreadable` / `already_swept` skips); NetBox has no atomic
tag add, so a tiny window remains.
`find_orphan_vms` returns an `OrphanCandidateList` (a list carrying the scan's
`skipped_reason`), so an empty result can be told apart from "could not verify".
`endpoint_ids` on `scan_vm_sidecar_orphan_candidates`, `find_orphan_vms` and
`run_orphan_vm_sweep` limits candidates to sidecars whose
`proxmox_endpoint_raw_id` is in the set: `None` is unscoped, an empty set matches
nothing, and a sidecar without a valid endpoint id is never a candidate when
scoped. Never widen an empty scope into an unscoped sweep.

`is_soft_deleted_vm()` is the single predicate for a decommissioned or
soft-deleted VM (status `decommissioning`, or the `proxbox-soft-deleted` tag; it
accepts nested `{"value": ...}` statuses, plain strings, and dict or string
tags). The virtual disk, snapshot, VM interface and VM IP stages drop such VMs
through `exclude_soft_deleted_vms()` and log one INFO line with the count. The
sweep itself and the VM stage must not filter them, so re-adoption still works.

## Cross-cluster guard and stale endpoint self-heal

- **What.** `(proxmox_endpoint_id, vmid)` is not a unique key across NetBox
  clusters. Every lookup keyed on it now requires the matched VM to live in the
  cluster being synchronized (`vm_cluster_guard.vm_record_in_cluster`: cluster id
  when both sides know it, else casefolded name; `vm_cluster_verdict` returns
  `match|mismatch|unassigned|unknown`. Explicit `cluster: null` rows
  (`unassigned`, legacy) and rows with no cluster data (`unknown`) are both
  rejected while the live cluster is known, and
  `vm_queue.skip_unverifiable_vm_candidates` (run before either engine by
  `build_vm_operation_queue`) skips the prepared VM instead of creating a
  duplicate unless a `(cluster id, vmid)` candidate exists; each skip is a
  structured stage warning from `vm_queue.unverifiable_vm_warnings`.
  `_load_netbox_virtual_machine_snapshot` completes every row missing `cluster`
  with at most 8 concurrent reads). It is applied in `reconciliation/vm_queue.select_existing_vm_record`
  (which also feeds sidecar hydration and the name pre-pass), in
  `sync_vm._resolve_vm_from_index_or_unique_vmid` (interfaces and IPs), in
  `snapshots._snapshot_sessions_for_vm` (only sessions of the VM's cluster), and
  as a post-pass over Rust-engine operations (re-resolved against the full snapshot). A dropped match logs a warning and
  is never written; the live cluster's own VM is still located when the first-wins
  index was shadowed by another cluster's record.
- **Stale-id self-heal.** The sidecar endpoint id is a database id (or a plugin
  primary key with `source=netbox`); those id spaces are independent and are
  reassigned when the proxbox-api database is recreated. When the
  `(endpoint, cluster, vmid)` sidecar lookup finds nothing,
  `sync_vm._resolve_vm_sidecar_identity` (only when that lookup proved the VM
  absent, never when it was ambiguous or unverifiable) calls
  `sync_state_reader.adopt_vm_with_stale_endpoint_id`, which retries by `(vmid, cluster)` and
  adopts the VM only if there is exactly one candidate, its sidecar type
  equals the live type, its sidecar cluster name is blank or equal, and its
  stored endpoint id is missing or not the id of any **configured** endpoint
  (database and NetBox plugin endpoints, enabled or not, loaded lazily once per run
  by `_ConfiguredEndpointIds`; a load failure means no adoption), and the live
  Proxmox name equals (casefolded) the NetBox VM name or the sidecar's
  `proxmox_vm_name`. The name match is a best-effort heuristic against a
  replacement VM reusing the VMID; a VM recreated under the same name is an
  accepted residual risk. It then
  rewrites the sidecar with `sync_state_writer.write_vm_endpoint_raw_id`; an
  unpersisted rewrite means no adoption. Anything ambiguous keeps the previous
  behavior. It is wired into sidecar hydration, so the name pre-pass and queue find the existing VM and neither add
  a ` (2)` suffix nor create a duplicate.
- **Why / downstream.** Before this, a stale or colliding endpoint id made the
  sidecar lookup reject or miss the VM, producing a half-populated duplicate VM,
  NICs/IPs on another cluster's VM, and skipped snapshot sync. The endpoint id
  space itself remains an operator/plugin concern. Individual-sync callers of
  `resolve_virtual_machine_by_sync_state` do not self-heal yet. The Rust engine
  still matches by endpoint key only; its output is filtered by the guard.
  Coverage: `tests/test_vm_stale_endpoint_adoption.py`,
  `tests/test_vm_cross_cluster_vmid.py`, `tests/test_vm_sync_reconciliation_queue.py`,
  `tests/test_snapshots_sync.py`.

## Extension Guidance

- Keep sync routines idempotent where possible.
- Every netbox-sdk accessor is `async def`. `await` it directly — never
  `asyncio.to_thread(lambda: <async call>)`, which yields an un-awaited
  coroutine. Make test fakes for SDK accessors coroutine functions too.
- Emit structured errors with `ProxboxException` for route-level handling.
- Keep progress reporting compatible with both WebSocket and SSE transport.
- Prefer small helper functions for object-specific concerns instead of growing a single coordinator module.
- For VM queue changes, update `tests/reconciliation/` and preserve
  Rust/Python parity before touching dispatch behavior.
