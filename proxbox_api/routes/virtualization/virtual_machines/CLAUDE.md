# proxbox_api/routes/virtualization/virtual_machines Directory Guide

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
- `sync_vm.py`: VM sync orchestration routes, including the create and stream entrypoints.

## How These Routes Work

- These handlers aggregate Proxmox cluster resources, VM configs, and NetBox object creation calls.
- They use sync decorators and extras dependencies for process tracking and custom fields.
- They write journal entries to NetBox for auditability of each synchronization run.
- Some paths stream progress over WebSocket or SSE, so those payloads must stay aligned.
- `sync_vm.py` also exposes the test route and the summary example route used by stub/coverage checks.

## Extension Guidance

- Extract large helper blocks into service modules when adding new sync paths.
- Keep WebSocket and non-WebSocket code paths behaviorally equivalent.
- Use `WebSocketSSEBridge` and `StreamingResponse` with `text/event-stream` for new stream endpoints.
- Keep read routes explicit about not-found and upstream-error behavior.
