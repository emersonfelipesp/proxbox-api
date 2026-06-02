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

## Extension Guidance

- Extract large helper blocks into service modules when adding new sync paths.
- Keep WebSocket and non-WebSocket code paths behaviorally equivalent.
- Use `WebSocketSSEBridge` and `StreamingResponse` with `text/event-stream` for new stream endpoints.
- Keep read routes explicit about not-found and upstream-error behavior.
- Do not reintroduce VM operation diffing in the route. Update
  `proxbox_api/services/sync/reconciliation/` and `tests/reconciliation/`
  instead.
