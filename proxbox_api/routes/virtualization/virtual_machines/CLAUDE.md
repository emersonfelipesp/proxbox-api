# proxbox_api/routes/virtualization/virtual_machines Directory Guide

## Purpose

Main synchronization endpoints for virtual machines and backups.

## Modules and Responsibilities

- `__init__.py`: Virtual machine sync routes and backup workflows.

## Key Data Flow and Dependencies

- Aggregates Proxmox cluster resources, VM configs, and NetBox object creation calls.
- Uses sync decorators and extras dependencies for process tracking and custom fields.
- Writes journal entries to NetBox for auditability of each synchronization run.

## Extension Guidance

- Extract large helper blocks into service modules when adding new sync paths.
- Maintain websocket and non-websocket code paths with equivalent behavior.
