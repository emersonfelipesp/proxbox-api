# proxbox_api/schemas Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/schemas/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Top-level Pydantic schema package for plugin and API contracts.

## Current Modules

- `__init__.py`: top-level schema exports and plugin configuration schema.
- `_base.py`: shared Proxbox base model.
- `proxmox.py`: Pydantic schemas for Proxmox sessions, cluster resources, node payloads, and resource payloads.
- `stream_messages.py`: typed stream event payload schemas used by SSE and WebSocket progress reporting.
- `netbox/`: NetBox session, endpoint, and payload schemas.
- `virtualization/`: VM config and summary schemas.

## How These Schemas Flow

- Route modules consume these schemas directly for request validation and response models.
- Session modules use them for connection and configuration payloads.
- Sync services rely on them as the contract boundary before any data is handed to NetBox or Proxmox clients.
- Streaming helpers in `proxbox_api/utils/streaming.py` and `proxbox_api/app/full_update.py` use `stream_messages.py` models to keep event shapes stable across transports.

## Extension Guidance

- Keep schema defaults explicit.
- Match upstream NetBox and Proxmox fields carefully so validation fails early and predictably.
- Put parsing and normalization in schema validators or computed fields rather than in route handlers.
