# proxbox_api/routes/dcim Directory Guide

## Purpose

Endpoints that synchronize and expose DCIM entities in NetBox.

## Current Files

- `__init__.py`: DCIM route handlers for device, interface, VLAN, and IP synchronization.

## Key Data Flow and Dependencies

- Consumes Proxmox-derived dependencies and sync services to create devices and interfaces.
- Depends on NetBox reconciliation helpers and `WebSocketSSEBridge` for stream responses.

## Extension Guidance

- Keep endpoint orchestration simple; place long-running sync logic in `services/sync`.
- Preserve response model declarations to maintain API contracts.
- When adding stream endpoints, use `WebSocketSSEBridge` and `StreamingResponse` with `text/event-stream`.
