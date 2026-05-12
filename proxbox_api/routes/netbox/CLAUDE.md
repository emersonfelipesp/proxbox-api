# proxbox_api/routes/netbox Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/netbox/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Endpoints for managing NetBox endpoint records and API diagnostics.

## Current Files

- `__init__.py`: NetBox route handlers for endpoint CRUD, status, and OpenAPI operations.

## How These Routes Work

- CRUD handlers operate on `NetBoxEndpoint` records via SQLModel sessions.
- Status and OpenAPI handlers use an established NetBox session dependency.
- The current model supports NetBox token v1 and v2 shapes (`token_version`, `token_key`, `token`).
- This package is where NetBox endpoint persistence meets the runtime connection state used by the rest of the app.

## Extension Guidance

- Keep database writes transactional and return clear HTTP exceptions.
- Avoid duplicating session setup logic; use `session/netbox` helpers.
- Keep secrets out of serialized response bodies.
