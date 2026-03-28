# proxbox_api/routes/dcim Directory Guide

## Purpose

Endpoints that synchronize and expose DCIM entities in NetBox.

## Modules and Responsibilities

- `__init__.py`: DCIM route handlers for device and interface synchronization.

## Key Data Flow and Dependencies

- Consumes Proxmox-derived dependencies and sync services to create devices and interfaces.
- Depends on local netbox-sdk compatibility wrappers for creation and serialization.

## Extension Guidance

- Keep endpoint orchestration simple; place long-running sync logic in services/sync.
- Preserve response_model declarations to maintain API contracts.
