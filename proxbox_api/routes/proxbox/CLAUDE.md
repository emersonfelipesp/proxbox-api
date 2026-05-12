# proxbox_api/routes/proxbox Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/proxbox/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Endpoints exposing Proxbox plugin configuration and settings views.

## Current Files

- `__init__.py`: Proxbox plugin route handlers for configuration access.

## How These Routes Work

- These handlers read plugin configuration from NetBox and map it into local Pydantic schemas.
- They expose `/netbox/plugins-config`, `/netbox/default-settings`, and `/settings`.
- They are not mounted by `create_app()` at the moment, so the routes remain opt-in.

## Extension Guidance

- Validate external configuration values before returning or using them.
- Keep optional NetBox imports isolated to the handlers that need them.
- Add new plugin-facing routes only when the data belongs to the Proxbox configuration surface.
