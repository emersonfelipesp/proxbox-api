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
- `proxmox_helpers.py`: typed Proxmox helper functions used by route orchestration and validated against generated models.
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
