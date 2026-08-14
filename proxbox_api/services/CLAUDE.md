# proxbox_api/services Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/services/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Reusable business workflows for synchronization, reconciliation, and Proxmox helper logic.

## Current Modules

- `__init__.py`: service package namespace.
- `custom_fields.py`: canonical NetBox custom-field inventory, reconcile/cache helpers, force-reconcile support, and object-type union preservation.
- `cloud_network.py`: managed customer-network settings resolver plus NetBox
  available-IP helpers used by Cloud QEMU/LXC provisioning.
- `auth_lockout.py`: shared, request-independent authentication lockout state
  service. It validates credential/source thresholds, window, row cap, separate
  per-bucket/global verification-concurrency capacity, and
  trusted-proxy configuration; derives source-plus-credential buckets without
  storing keys or dictionary-testable fingerprints; inserts one durable
  per-token reservation before bcrypt; and gives each row a renewable expiry
  capped by a persisted absolute deadline.
  Expired crash rows stop consuming capacity and remain observable for one hour;
  a result can update accounting exactly once only before its terminal deadline,
  and later results are discarded. Admission and finalization both compact older
  rows into a durable aggregate counter. Rejection converts the consumed token
  to failure state. Same-credential cohort completions coalesce the credential
  transition, but every consumed rejection advances source-abuse accounting.
  Missing-key requests allocate no credential row and IPv6 sources aggregate at
  `/64`. Credential/source failure rows use independent bounded partitions.
  Failure-row saturation never controls admission: all requests remain inside
  the same per-source/global verification pool. Once a reservation deadline
  passes, capacity is reclaimable and a late bcrypt result is discarded without
  changing lockout state; the non-preemptible worker thread may retain residual
  CPU cost until bcrypt returns. A saturated partition first
  evicts its stalest safe expired row; an unpersistable rejection fails closed
  and advances bounded aggregate accounting. Reservation owners renew a fixed
  lease while bcrypt is live but never beyond the terminal lifetime, and each
  request has a bounded active-key scan. The service persists
  label-free capacity/row/in-flight/orphan/compaction metrics and provides safe local
  inspection/clear selectors. Startup pins the validated HMAC generation in
  memory; do not re-read a mutable key source on request paths.
- `proxmox_helpers.py`: typed Proxmox helper functions used by route orchestration and validated against generated models. VM config validation uses copy-on-write normalization for blank optional booleans and for integer/float values returned for optional string fields (including aliases), while preserving booleans and required string fields; this keeps QEMU values such as numeric `memory` compatible with the generated response contracts without changing upstream payloads.
- `packer_preflight.py`: endpoint-scoped Cloud Image Pipeline readiness checks.
  It accepts one already-resolved Proxmox session, performs only GET calls for
  node status, provider-derived storage capabilities, and VMID availability,
  and returns typed, secret-free findings. `cluster/nextid?vmid=` is the VMID
  authority; `cluster/resources` is supplemental because RBAC can hide rows.
  It preserves valid-empty versus malformed collection semantics so malformed
  payloads fail closed, and it requires affirmative storage health state. It
  must remain usable when endpoint writes are disabled.
- `packer_plans.py`: issues and verifies short-lived HMAC-bound Cloud Image
  Pipeline execution plans. Token segments use unpadded canonical Base64URL;
  verification rejects padding, invalid characters, and alternate encodings
  whose unused padding bits decode to the same bytes, then binds the plan to
  the current endpoint authority, target, recipe, expiry, and durable lease.
- `hardware_discovery.py`: opt-in SSH node hardware discovery
  (`dmidecode`/`ip`/`ethtool` allowlist). `reflect_to_netbox()` writes
  chassis/NIC custom fields and, via `_reflect_nic_mac()`, the physical-NIC MAC
  as a native `dcim.MACAddress` plus `primary_mac_address`. It reuses
  `sync/mac_address.py::reconcile_mac_for_interface` so physical and
  bridge/bond MACs are stored identically and receive the same Proxbox tag.
  The native MAC write requires both
  `hardware_discovery_enabled` and the separate default-off
  `hardware_discovery_sync_nic_macs` UI setting. A missing setting is false; a
  MAC failure warns and never aborts the run.
- `zfs.py`: tiered ZFS storage retrieval for `netbox-proxbox` consumers. Tier 1 parses only structured Proxmox REST responses from `/nodes/{node}/disks/zfs` and `/nodes/{node}/disks/zfs/{name}`. Tier 2 (InfluxDB) and Tier 3 (JSON-native SSH CLI) are clean fallback seams that currently degrade gracefully; any future SSH implementation must resolve the endpoint row and pass the existing `access_methods="api_ssh"` gate before opening a transport.
- `sync/`: main synchronization workflows for clusters, devices, virtual machines, storage, backups, snapshots, disks, interfaces, IPs, and task history.
- `sync/reconciliation/`: pure operation-queue builders, including the VM queue
  Python fallback and optional Rust bridge.
- `sync/individual/`: targeted single-object sync workflows with dependency auto-creation and dry-run support.

## How Services Are Used

- Route handlers import these modules to keep HTTP, SSE, and WebSocket code thin.
- `session/` provides the authenticated clients that service functions consume.
- `schemas/` and `proxmox_to_netbox/` provide the normalization layer that services rely on.
- VM full sync uses `sync/reconciliation/build_vm_operation_queue()` as the
  synchronous boundary between prepared desired state and NetBox write dispatch.
- Task-history bulk sync is node-oriented, not VM-oriented: one paginated
  archive walk per selected node feeds one NetBox reconcile. The VM route owns
  only the backward-compatible `sync_task_history` flag; the service owns
  identity safety, UPID dedupe, cancellation, and degraded/error reporting.

## Extension Guidance

- Keep service functions independent from request objects where possible.
- Prefer idempotent operations so repeated sync runs are safe.
- Keep NetBox custom fields declared only in `custom_fields.py`; both startup bootstrap and extras routes import the same inventory object.
- Custom-field reconcile must preserve operator-added `object_types`: use the
  live lookup record for both the object-type union and reconcile diff, and
  fail the field on lookup errors rather than sending a declared-only
  `object_types` payload.
- Custom-field reconcile must only cache records verified from NetBox. If the
  shared REST reconciler returns an unverified/synthetic record without a
  NetBox-assigned `id`, report that custom field as failed and leave the
  process-local custom-field cache empty.
- Surface predictable errors through `ProxboxException`.
- Keep response payloads compatible with both JSON and stream transports when a service is reused in SSE or WebSocket paths.
- Cloud-network helpers must use `proxbox_api.netbox_rest` with an existing
  NetBox session. `peek_available_ips(prefix_id, limit)` GETs
  `/api/ipam/prefixes/{id}/available-ips/` and never occupies addresses;
  `allocate_ip(prefix_id, *, vminterface_id=None, status="active")` POSTs the
  same NetBox endpoint to atomically occupy one address and can bind it to a
  `virtualization.vminterface`; `release_ip(ip_id)` deletes the IPAddress
  best-effort for provisioning rollback.
- Keep reconciliation seams pure: no HTTP clients, async I/O, database writes,
  retry loops, or stream emission inside queue builders.
- Keep Packer preflight strictly read-only: no POST/PUT/PATCH/DELETE helpers,
  task dispatch, SSH, database mutation, or `allow_writes` rejection. Treat
  image storage as `iso` only for `proxmox_iso`; release/source providers use
  private host staging and make no image-storage claim. Treat VM storage as
  `images`, and derive snippets solely from the normalized provider target.
  `content=import` belongs to the separate download-url POST and is not a
  storage capability. Never promote a denied or malformed authoritative VMID
  probe to success based on resource enumeration.
