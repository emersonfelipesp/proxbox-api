# proxbox_api/routes/dcim Directory Guide

## Purpose

Endpoints that synchronize and expose DCIM entities in NetBox.

## Current Files

- `__init__.py`: DCIM route handlers for device discovery, device creation, interface creation, VLAN/IP reconciliation, and SSE stream variants.

## How These Routes Work

- Route handlers consume Proxmox-derived dependencies and sync services to create or update NetBox DCIM objects.
- They depend on NetBox reconciliation helpers and `WebSocketSSEBridge` for streamed progress responses.
- The route layer should stay thin and defer the object-specific workflow to `services/sync`.

## Extension Guidance

- Keep endpoint orchestration simple.
- Preserve response model declarations so API contracts stay stable.
- Use `WebSocketSSEBridge` and `StreamingResponse` with `text/event-stream` for new stream endpoints.
