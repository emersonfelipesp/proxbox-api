# proxbox_api Directory Guide

## Purpose

Core FastAPI package for `proxbox-api`. This package owns application composition, route registration, client session factories, schemas, services, generated artifacts, and shared helpers.

## Package Map

- `app/` — application factory, bootstrap, CORS, exception handlers, cache routes, root metadata, full-update orchestration, and WebSocket handlers. See `app/CLAUDE.md`.
- `routes/` — FastAPI route packages for admin, NetBox, Proxmox, DCIM, virtualization, Proxbox plugin access, and sync helpers. See `routes/CLAUDE.md`.
- `services/` — synchronization workflows and reusable helper logic, including the typed Proxmox helper layer. See `services/CLAUDE.md`.
- `session/` — NetBox and Proxmox session factories, providers, and dependency aliases. See `session/CLAUDE.md`.
- `schemas/` — Pydantic request and response models for external and internal contracts. See `schemas/CLAUDE.md`.
- `enum/` — Proxmox and NetBox choice values used by schemas and routes. See `enum/CLAUDE.md`.
- `proxmox_codegen/` — crawler and generator pipeline that produces Proxmox contract artifacts. See `proxmox_codegen/CLAUDE.md`.
- `proxmox_to_netbox/` — schema-driven transformation from Proxmox payloads to NetBox payloads. See `proxmox_to_netbox/CLAUDE.md`.
- `generated/` — checked-in generated OpenAPI, model artifacts, and runtime route cache data. See `generated/CLAUDE.md`.
- `types/` — shared type aliases and protocol definitions. See `types/CLAUDE.md`.
- `utils/` — streaming, retry, logging, error handling, and WebSocket helper utilities. See `utils/CLAUDE.md`.
- `e2e/` — browser-backed test helpers and fixtures. See `e2e/CLAUDE.md`.
- `custom_objects/` — reserved area for custom NetBox object wrappers. See `custom_objects/CLAUDE.md`.
- `diode/` — experimental Diode sandbox integration. See `diode/CLAUDE.md`.
- `templates/` — Jinja2 templates used by the admin route.
- `static/` — static assets bundled with the package.
- `test_*.py` — package-level smoke tests that run with the repository test suite.

## Runtime Boundaries

- `proxbox_api.app.factory.create_app()` is the application assembly point. It initializes bootstrap state, registers middleware, mounts root/cache/full-update/WebSocket routes, and exposes the `app` object imported by `proxbox_api.main`.
- `database.py` persists NetBox and Proxmox endpoint records in SQLite and feeds bootstrap/session creation.
- `session/netbox.py` and `session/proxmox.py` own client construction and dependency wiring. Route handlers should use these dependencies instead of creating clients inline.
- `services/sync/` and `routes/virtualization/virtual_machines/` handle the main Proxmox-to-NetBox sync flow, including per-object journal tracking and stream progress.
- `proxmox_to_netbox/` is the normalization boundary. Parsing and conversion must happen in schemas and mappers, not in route handlers.

## Key Data Flow

1. Startup bootstraps the local database and default NetBox session unless bootstrap is skipped.
2. Routes resolve NetBox or Proxmox clients through dependency aliases.
3. Service modules fetch source data, normalize it through schemas, and create or update NetBox objects.
4. Route handlers translate those workflows into HTTP, SSE, or WebSocket responses.
5. Generated Proxmox routes are mounted at lifespan startup and may fail open or fail closed depending on `PROXBOX_STRICT_STARTUP`.

## Extension Guidance

- Keep route modules thin and move reusable logic into services or utility modules.
- Add new request and response models to `schemas/` before wiring route code.
- Keep generated artifacts and contract snapshots out of manual edits unless you are debugging the generator.
- Preserve ASCII-only documentation and source text unless a file already requires otherwise.
- Prefer `ProxboxException` for expected API failures and `logger` for operational messages.
- When adding new sync behavior, keep WebSocket and SSE payload shapes aligned.
