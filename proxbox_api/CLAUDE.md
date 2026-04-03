# proxbox_api Directory Guide

## Purpose

Core FastAPI package for `proxbox-api`. This package owns application composition, route registration, client session factories, schemas, services, generated artifacts, and shared helpers.

## Package Map

- `app/`: application factory, bootstrap, CORS, exceptions, cache routes, root metadata, SSE bridge wiring, and WebSocket handlers.
- `routes/`: FastAPI route packages for admin, NetBox, Proxmox, DCIM, virtualization, Proxbox plugin access, and sync helpers.
- `services/`: synchronization workflows and reusable helper logic.
- `session/`: NetBox and Proxmox session factories and dependency providers.
- `schemas/`: Pydantic request and response models for external and internal contracts.
- `enum/`: Proxmox and NetBox choice values used by schemas and routes.
- `proxmox_codegen/`: crawler and generator pipeline that produces Proxmox contract artifacts.
- `proxmox_to_netbox/`: schema-driven transformation from Proxmox payloads to NetBox payloads.
- `generated/`: checked-in generated OpenAPI and model artifacts.
- `e2e/`: browser-backed test helpers and fixtures.
- `utils/`: streaming, retry, logging, error handling, and WebSocket helper utilities.
- `custom_objects/`: reserved area for custom NetBox object wrappers.
- `diode/`: experimental Diode sandbox integration.
- `templates/`: Jinja2 templates used by the admin route.
- `types/`: shared aliases and protocol definitions.
- `static/`: static assets bundled with the package.
- `test_*.py`: package-level smoke tests that run with the repository test suite.

## Runtime Boundaries

- `proxbox_api.app.factory.create_app()` is the application assembly point. It initializes bootstrap state, registers middleware, mounts routers, and exposes the `app` object imported by `proxbox_api.main`.
- `database.py` persists NetBox endpoint records in SQLite and feeds bootstrap/session creation.
- `session/netbox.py` and `session/proxmox.py` own client construction. Route handlers should use these dependencies instead of creating clients inline.
- `services/sync/` and `routes/virtualization/virtual_machines/` handle the main Proxmox-to-NetBox sync flow, including per-object journal tracking and stream progress.
- `proxmox_to_netbox/` is the normalization boundary. Parsing and conversion must happen in schemas and mappers, not in route handlers.

## Key Data Flow

1. Startup bootstraps the local database and default NetBox session.
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
