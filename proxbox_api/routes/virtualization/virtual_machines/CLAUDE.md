# proxbox_api/routes/virtualization/virtual_machines Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Main synchronization endpoints for virtual machines and related resources.

## Current Files

- `__init__.py`: virtual machine sync route aggregation and export surface.
- `read_vm.py`: read, query, and interface/IP routes for VMs.
- `backups_vm.py`: backup reconciliation helpers and routes.
- `disks_vm.py`: VM disk reconciliation helpers and routes.
- `helpers.py`: shared VM route helpers and concurrency helpers.
- `snapshots_vm.py`: snapshot reconciliation helpers and routes.
- `storages_vm.py`: storage reconciliation helpers and routes.
- `sync_vm.py`: VM sync orchestration routes, including the create and stream
  entrypoints. Deterministic operation-queue reconciliation is delegated to
  `proxbox_api.services.sync.reconciliation`.

## How These Routes Work

- These handlers aggregate Proxmox cluster resources, VM configs, and NetBox object creation calls.
- They use sync decorators and extras dependencies for process tracking and custom fields.
- They write journal entries to NetBox for auditability of each synchronization run.
- Some paths stream progress over WebSocket or SSE, so those payloads must stay aligned.
- `sync_vm.py` also exposes the test route and the summary example route used by stub/coverage checks.
- Full VM sync prepares desired VM state and the NetBox snapshot here, but queue
  classification (`CREATE`, `GET`, `UPDATE`) belongs to the reconciliation
  service seam.

## Behavior Notes

- **Blank-name VM recovery.** `_create_virtual_machine_by_netbox_id` matches a
  NetBox VM to Proxmox by name **or** `proxmox_vm_id`. It only rejects (HTTP
  422) a VM that has neither a name nor a `proxmox_vm_id` custom field — a
  blank-name record with a known `proxmox_vm_id` is matched by vmid and its
  name is healed from the matched Proxmox resource on the next sync.
- **Interface failures are surfaced, not swallowed.** Per-interface creation is
  retried a bounded number of times for transient NetBox errors; interfaces
  that still fail are counted. The per-VM progress item carries
  `failed_interfaces` and `total_interfaces`, and a VM with any failed interface
  is reported with `status="warning"` (degraded) instead of `completed`. Keep
  the WebSocket and SSE item payloads aligned when changing these fields.
- **VM batch failures are counted, not reported as success (issue #563).**
  `_run_full_update_vm_batch` returns `(results, failed_vms)`. A VM that raises
  during preparation or fails to resolve increments `failed_vms`; the caller
  computes `total_vms = len(results) + failed_vms` so a stage where every VM
  failed reports `total>0, failed>0` instead of the misleading
  `total=0 ok=0 failed=0` that previously let a fully-failed stage look
  "completed". When changing the batch contract, keep the failure count flowing
  into the stage summary so multi-endpoint mis-scoping can never masquerade as
  an empty-but-successful run.
- **Full-update VM config fetch is a separate phase.** `_run_full_update_vm_batch`
  first fetches all Proxmox VM configs under the VM fetch semaphore, and that
  semaphore covers only `get_vm_config`. After every config fetch has completed,
  the batch processes successful configs into `_PreparedVMState` objects. Keep
  CPU validation/payload building and NetBox dependency calls out of the fetch
  semaphore so pending Proxmox HTTP responses are drained promptly and aiohttp
  wall-clock timeouts do not fire while other slots are doing CPU or NetBox work.
- **VM lookups are scoped by `(cluster_id, vmid)`, not vmid alone (issue #223).**
  Proxmox `vmid` is only unique within one cluster, so the same `vmid` can exist
  on several clusters. The VM snapshot index is keyed by the
  `(NetBox cluster id, proxmox_vm_id)` tuple
  (`_build_vm_index_by_proxmox_id`), and interface/IP sync resolve their NetBox
  VM through `_resolve_vm_from_index_or_unique_vmid` and
  `_resolve_netbox_virtual_machine_by_proxmox_id`, both of which take a
  `cluster_id`/`cluster_name`. The NetBox cluster id is resolved by name once
  per cluster via `resolve_netbox_cluster_id_by_name`
  (`services/sync/vm_helpers.py`) and memoized in a per-run cache. When the
  cluster cannot be resolved, the code falls back to a vmid-only lookup **only
  when it is globally unambiguous** (exactly one NetBox VM matches); an
  ambiguous vmid is logged and skipped rather than mapped to the wrong VM. This
  prevents interfaces/IPs from attaching to a same-vmid VM on another cluster
  and is why the interface-collection loop also filters Proxmox resources by
  `cluster_name`. Regression coverage: `tests/test_vm_cross_cluster_vmid.py`.
- **VM create routes bootstrap NetBox dependencies before writing.** The
  `/create`, `/{netbox_vm_id}/create`, `/create/stream`, and
  `/{netbox_vm_id}/create/stream` handlers attach the
  `ensure_netbox_sync_dependencies` FastAPI dependency. It re-runs the
  idempotent NetBox bootstrap for Proxbox-owned support objects on each sync
  request, so missing discovery tags, VM roles/types, device roles/types,
  cluster types, and custom fields are recreated before payloads reference
  them by slug.

- **VM and template sync modes (`sync_mode_vm`, `sync_mode_vm_template`).** The
  `create_virtual_machines` route accepts two optional query parameters that
  control whether non-template VMs and template VMs are included in a given
  sync pass.  Accepted values: ``"always"`` (default), ``"bootstrap_only"``
  (treated as enabled at the backend), ``"disabled"`` (all matching resources
  are skipped for this pass without counting as failures).  A Proxmox resource
  is identified as a template when its ``template`` field is truthy (``1``,
  ``"1"``, ``True``).  Filtering is applied **at the source** by
  `_filter_cluster_resources_by_sync_modes` immediately after the
  `netbox_vm_ids` filter, *before* discovery and dependency precompute — so a
  ``"disabled"`` mode does not create/update dependent NetBox objects
  (manufacturer, device type, cluster, site, node devices, VM roles) for VMs
  that will never sync. A single INFO summary logs how many resources were
  dropped. Filtered records do NOT increment ``failed_vms``. The stream wrapper
  (`create_virtual_machines_stream`) forwards both params to the inner function.
  Unknown values fall back to ``"always"`` with a WARNING. Coverage:
  `tests/test_vm_sync_modes.py`.

- **Per-VM dispatch isolation.** `_dispatch_vm_operation_queue` returns
  ``(resolved_records, failed_keys)``: a single VM's create/update failure is
  logged and its key added to ``failed_keys`` (the caller counts it against
  ``failed_vms``) instead of raising and aborting the whole queue, so one bad VM
  no longer drops every VM queued after it. A dispatch-failed VM is never masked
  as success even when a stale existing record is present. Coverage:
  `tests/test_vm_sync_reconciliation_queue.py`.

- **Concurrent VM operation dispatch.** `_dispatch_vm_operation_queue` runs all
  queued CREATE/UPDATE/GET operations concurrently via `asyncio.gather`, bounded
  by an `asyncio.Semaphore` whose width comes from `resolve_netbox_write_concurrency()`
  (default 8, env `PROXBOX_NETBOX_WRITE_CONCURRENCY`). The previous serial
  batch-per-batch loop is replaced — all operations are dispatched at once and
  the semaphore serialises them to the configured concurrency cap. Per-VM failure
  isolation is unchanged; the semaphore context is entirely inside the per-VM
  error handler so one VM's failure releases the semaphore slot immediately.

- **Single `netbox_version` per sync pass.** `detect_netbox_version` is called
  once at the start of `create_virtual_machines` and the result is threaded
  through to every `ensure_vm_type` invocation via the `netbox_version=` keyword
  argument (added in `proxbox_api/services/sync/vm_create.py`). Previously
  `ensure_vm_type` called `detect_netbox_version` independently on every
  invocation, adding one NetBox round-trip per VM type per sync.

- **Parallel cluster dependency precomputation.** `_precompute_vm_dependencies`
  processes all clusters concurrently via `asyncio.gather` instead of
  sequentially. Within each cluster, `_ensure_cluster_type`, `_ensure_site`, and
  `_resolve_tenant` are mutually independent and are gathered in parallel before
  `_ensure_cluster` (which depends on all three). Node device ensures remain
  sequential within a cluster (they depend on the resolved cluster id). Any
  cluster-level failure is still surfaced — the first `BaseException` in the
  gather results is re-raised so the outer `try/except` in `create_virtual_machines`
  can wrap it as a `ProxboxException`.

- **Cluster site scope is authoritative for dependent writes.** After
  `_ensure_cluster` reconciles a NetBox cluster, VM sync uses the returned
  cluster's `dcim.site` scope as the site for node-device and VM payloads,
  falling back to the endpoint/default site only when the cluster has no site
  scope. This prevents NetBox from rejecting devices or VMs with "assigned
  cluster belongs to a different site" when an existing cluster is scoped to a
  different site than the endpoint resolver returned. Regression coverage:
  `tests/test_vm_sync_two_phase.py::test_full_update_uses_reconciled_cluster_site_scope`.

- **Interface-dense guests (guest-agent payloads).** Guest-agent
  `network-get-interfaces` calls use a dedicated timeout
  (`PROXBOX_GUEST_AGENT_TIMEOUT` / plugin key `guest_agent_timeout`, default
  15 s) with one bounded retry on timeout, because enumerating 100+ interfaces
  (VRRP routers) is slow in-guest and the global Proxmox session timeout
  (5 s default) silently dropped guest data. The timeout override
  (`_scoped_proxmox_backend_timeout`) only ever **widens** the shared backend's
  `total` (depth-counted so overlapping calls restore the true original on the
  last exit) and preserves the other `ClientTimeout` fields. Alias entries
  (`name:N`) are matched to their parent **by name** (not by MAC) during
  normalization (`_normalize_guest_agent_interfaces`) so genuine distinct
  interfaces that share a MAC (real VRRP virtual MACs) are never conflated;
  alias addresses are merged into the parent and deduped. A VM-interface
  **bulk** reconciliation that fails *or* returns partial failures
  (`result.failed > 0`) now raises (`ProxboxException`) and emits a failed/end
  frame on the stream instead of returning a silent empty/partial success.
  Regression coverage: `tests/test_interface_dense_vm_sync.py`.

- **Sparse Proxmox network config keys.** QEMU config payloads can legitimately
  expose `net1`, `net2`, or higher `net<N>` keys without a `net0` entry. VM
  interface sync must iterate exact `net<N>` keys present in the payload and
  sort them by numeric suffix; never walk from `net0` until the first gap.
  Prefix lookalikes such as `netboot` and `running-nets-host-mtu` are not VM
  NIC config entries. Regression coverage:
  `tests/test_vm_network_config_parsing.py`.

## Extension Guidance

- Extract large helper blocks into service modules when adding new sync paths.
- Keep WebSocket and non-WebSocket code paths behaviorally equivalent.
- Use `WebSocketSSEBridge` and `StreamingResponse` with `text/event-stream` for new stream endpoints.
- Keep read routes explicit about not-found and upstream-error behavior.
- Do not reintroduce VM operation diffing in the route. Update
  `proxbox_api/services/sync/reconciliation/` and `tests/reconciliation/`
  instead.
