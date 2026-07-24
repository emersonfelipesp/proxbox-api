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

- **Targeted-sync NetBox lookups must be `await`ed (netbox-proxbox #616).**
  Every netbox-sdk accessor is `async def` — there is no synchronous `get()`
  anywhere in the SDK. The four targeted single-object routes
  (`sync_vm.py::_create_virtual_machine_by_netbox_id`, `backups_vm.py`,
  `disks_vm.py`, `snapshots_vm.py`) resolve the NetBox VM row with
  `await netbox_session.virtualization.virtual_machines.get(id=...)`.
  Historically they did not: `sync_vm.py` called it bare and the other three
  wrapped it in `asyncio.to_thread(lambda: ...)` (which runs the *coroutine
  factory* in a worker thread and returns an un-awaited coroutine). Both
  produced a coroutine instead of a `Record`, `to_mapping()` degraded it to
  `{}`, and every targeted sync failed with *"Virtual machine id=N has no name
  and no proxmox_vm_id custom field to match in Proxmox"* while full-cluster
  sync — which never resolves a NetBox row by PK — worked fine. Never wrap an
  async SDK call in `asyncio.to_thread`; just `await` it. Regression coverage:
  `tests/test_targeted_vm_sync_await.py` (includes an AST contract over all
  four modules). Test fakes for `virtual_machines.get` **must be coroutine
  functions** — a synchronous fake is what let this ship.
- **Exact targeted VM ownership.** `_create_virtual_machine_by_netbox_id`
  and the selected batch filter join every selected core VM to the typed
  `ProxboxVMSyncState` sidecar once, then match only its Proxmox endpoint,
  normalized cluster name, positive VMID, and guest type. Sidecar identity is
  authoritative even when legacy custom fields are stale. Legacy custom-field
  fallback is allowed only when the sidecar row is absent/unavailable and
  `custom_fields_enabled=true`; malformed or duplicate relevant sidecars fail
  closed. VM names are never selectors, so a same-name guest, a reused VMID on
  another endpoint/cluster, or the wrong QEMU/LXC type cannot widen the
  operation. A blank-name record can still heal its name when that complete
  identity is present. The same sidecar-first selection contract applies to
  explicitly selected backup sync: `backups_vm.py::_prefetch_vm_cache` overlays
  sidecar identity onto every selected VM (via
  `vm_filter.hydrate_selected_vm_identities`) before validating exact
  endpoint/cluster/VMID scope. The by-id snapshot and backup stream routes have
  no legacy `proxmox_vm_id` custom-field precondition — ownership resolves
  downstream from the sidecar, so sidecar-only VMs (the default with
  `custom_fields_enabled=false`) sync through them.
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
- **VM lookups are scoped by `(proxmox_endpoint_id, vmid)` first (issue #255).**
  Proxmox `vmid` values can repeat across standalone endpoints, even when those
  endpoints have no shared NetBox cluster identity. The VM snapshot index is
  keyed by `(proxmox_endpoint_id, proxmox_vm_id)` via
  `_build_vm_index_by_proxmox_id`, and full VM sync, interface sync, IP sync,
  individual VM sync, and the reconciliation queue all prefer
  `cf_proxmox_endpoint_id + cf_proxmox_vm_id`. Cluster-scoped matching remains
  only as legacy fallback when no endpoint id is available. A VMID-only fallback
  is allowed only for one legacy NetBox VM that has no endpoint id; ambiguous
  VMIDs are logged and skipped instead of being mapped to the wrong endpoint.
  Disk and snapshot sync must route config/list calls through the Proxmox
  session whose endpoint id matches the NetBox VM, so a same-VMID guest on
  another endpoint cannot supply disks or snapshots. Regression coverage:
  `tests/test_vm_cross_cluster_vmid.py`, `tests/test_virtual_disks_sync.py`,
  `tests/test_snapshots_sync.py`, and `tests/reconciliation/test_vm_queue_python.py`.
- **VM create routes bootstrap NetBox dependencies before writing.** The
  `/create`, `/{netbox_vm_id}/create`, `/create/stream`, and
  `/{netbox_vm_id}/create/stream` handlers attach the
  `ensure_netbox_sync_dependencies` FastAPI dependency. It re-runs the
  idempotent NetBox bootstrap for Proxbox-owned support objects on each sync
  request, so missing discovery tags, VM roles/types, device roles/types,
  cluster types, and custom fields are recreated before payloads reference
  them by slug.

- **Task-history has one owner per request.** `/create`, `/create/stream`, and
  both targeted create routes declare `sync_task_history` as a real FastAPI
  boolean query parameter with default `true`. When enabled, the route invokes
  one aggregate after it knows the successfully reconciled NetBox VM IDs; it
  never invokes task history from the per-VM worker. Full-update passes `false`
  because its dedicated task-history stage runs once afterward. Keep the flag
  forwarded through both targeted and SSE call chains so `false` cannot be
  silently dropped and omission preserves standalone compatibility.
  When the standalone/default-true path owns task history, propagate fatal
  `ProxboxException` outcomes so neither REST nor SSE can report VM-route
  success after an unpaired/failed task-history stage. A degraded aggregate is
  also HTTP 502 for standalone REST after its safe rows are retained; SSE keeps
  the task-history warning summary visible. Selected-ID resource filtering must
  use deduplicated, bounded repeated `id` parameters and fail closed if any
  chunk lookup fails.
  The dedicated `/task-history/create/stream` route also declares
  `netbox_vm_ids`; omission means all VMs, while an explicitly empty, malformed,
  or non-positive string returns ordinary HTTP 422 before SSE starts so a bad
  scoped request cannot widen to the whole estate. It must reset sidecar
  availability memoization per request
  and validate `fetch_max_concurrency >= 1`.

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
  **bulk** reconciliation that fails systemically still raises, but per-record
  partial failures (`result.failed > 0`) log failed VM/interface payloads,
  return successful records, and emit warning summaries so full-update can
  continue into VM IP sync. Guest-agent-derived core VMInterface names are
  sanitized and capped to NetBox's 64-character name field before write.
  Regression coverage: `tests/test_interface_dense_vm_sync.py` and
  `tests/test_vm_sync.py`.

- **Sparse Proxmox network config keys.** QEMU config payloads can legitimately
  expose `net1`, `net2`, or higher `net<N>` keys without a `net0` entry. VM
  interface sync must iterate exact `net<N>` keys present in the payload and
  sort them by numeric suffix; never walk from `net0` until the first gap.
  Prefix lookalikes such as `netboot` and `running-nets-host-mtu` are not VM
  NIC config entries. Regression coverage:
  `tests/test_vm_network_config_parsing.py`.

- **IP sync requires the interface to already exist; missing ones are surfaced.**
  `_sync_vm_ips` attaches an IP only to a VM interface that already exists in
  NetBox (looked up by resolved name in `interface_name_to_id`). When the
  interface is absent it increments a per-VM `missing_interface_count`, logs a
  **WARNING** (not DEBUG), and — on an SSE run — emits a
  `emit_phase_summary(phase="vm-ip-addresses", skipped=N, …)` so an IP-only sync
  whose interfaces are stale/missing is diagnosable instead of silently
  reconciling nothing. The interface stage (`create_only_vm_interfaces`) does
  **not** create IPs, so the IP stage depends on the interface stage having run
  first; the `netbox-proxbox` plugin now auto-runs the VM-interface stage before
  the IP-address stage. Coverage:
  `tests/test_qemu_guest_agent_sync.py::test_vm_only_ip_sync_surfaces_missing_interface_skip`.

- **VM interface/IP stream route ownership.** The public
  `/interfaces/create/stream` and `/interfaces/ip-address/create/stream`
  handlers are registered from `read_vm.py`; `interfaces_vm.py` is only a
  compatibility import module for older code references. Keep
  `vm_interface_sync_strategy` and related query params on those registered
  handlers, and avoid reintroducing duplicate registered routes for the same
  paths. Coverage:
  `tests/test_guest_vm_interface_sync.py::test_registered_stream_routes_expose_strategy_param`.

- **Proxmox-authoritative VM rename via the last-synced-name truth table
  (netbox-proxbox #617).** When a VM's name differs between Proxmox and the
  stored NetBox record, sync no longer unconditionally preserves the NetBox
  name. It decides using evidence recorded in the `ProxboxVMSyncState` sidecar
  (`proxmox_vm_name` = the Proxmox name observed on the previous sync), read via
  `services/sync/sync_state_reader.py` (`load_vm_last_synced_name(s)`) and
  written via `services/sync/sync_state_writer.py`:
  - stored NetBox name **==** last-synced sidecar name but **!=** incoming
    Proxmox name ⇒ Proxmox was renamed ⇒ **UPDATE** the NetBox name to match;
  - stored NetBox name **!=** last-synced sidecar name ⇒ a human edited the
    NetBox name ⇒ **KEEP** the NetBox name (operator intent wins);
  - no/blank/ambiguous sidecar evidence ⇒ **fall back to name-preserving**
    behavior (fail-safe — a missing sidecar can never trigger an unwanted
    rename). There is no toggle; the fail-safe default reproduces the old
    behavior whenever evidence is absent.
  The decision runs in `_resolve_vm_names_pre_pass` (`sync_vm.py`), which is
  invoked from **two** call sites and applied on **every** name-writing path:
  the batch `_run_full_update_vm_batch` path, and the `sync_vm_network=True`
  `create_vm_task` path (via `name_prepass_vms` / `default_resolved_vm_names`,
  with the resolved name overriding the payload before dispatch). The sidecar
  `proxmox_vm_name` evidence is refreshed on each sync whenever the observed
  Proxmox name is non-blank, independent of the `overwrite_custom_fields` flag,
  so the truth table always has fresh evidence to compare against next run.
  Coverage: `tests/test_vm_sync_two_phase.py`
  (`test_full_update_batch_applies_proxmox_rename_when_sidecar_matches_stored_name`,
  `test_full_update_batch_preserves_operator_rename_when_sidecar_differs`,
  `test_full_update_batch_preserves_netbox_name_when_sidecar_name_is_blank`).

## Extension Guidance

- Extract large helper blocks into service modules when adding new sync paths.
- Keep WebSocket and non-WebSocket code paths behaviorally equivalent.
- Use `WebSocketSSEBridge` and `StreamingResponse` with `text/event-stream` for new stream endpoints.
- Keep read routes explicit about not-found and upstream-error behavior.
- Do not reintroduce VM operation diffing in the route. Update
  `proxbox_api/services/sync/reconciliation/` and `tests/reconciliation/`
  instead.
