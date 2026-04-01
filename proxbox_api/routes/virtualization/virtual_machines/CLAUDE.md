# proxbox_api/routes/virtualization/virtual_machines Directory Guide

## Purpose

Main synchronization endpoints for virtual machines and backups.

## Current Files

- `__init__.py`: Virtual machine sync routes and backup workflows.
- `read_vm.py`: Read, query, and sync routes for VMs.
- `backups_vm.py`: Backup reconciliation helpers and routes.
- `disks_vm.py`: VM disk reconciliation helpers and routes.
- `helpers.py`: Shared VM route helpers.
- `snapshots_vm.py`: Snapshot reconciliation helpers and routes.
- `storages_vm.py`: Storage reconciliation helpers and routes.
- `sync_vm.py`: VM sync orchestration routes.

## Key Data Flow and Dependencies

- Aggregates Proxmox cluster resources, VM configs, and NetBox object creation calls.
- Uses sync decorators and extras dependencies for process tracking and custom fields.
- Writes journal entries to NetBox for auditability of each synchronization run.

## Extension Guidance

- Extract large helper blocks into service modules when adding new sync paths.
- Maintain websocket and non-websocket code paths with equivalent behavior.
- When adding stream endpoints, use `WebSocketSSEBridge` and `StreamingResponse` with `text/event-stream`.
