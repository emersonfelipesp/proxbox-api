# proxbox_api Directory Guide

## Purpose

Core FastAPI package for proxbox-api. This package owns app composition, route registration, session factories, schemas, services, generated artifacts, and shared helpers.

## Modules and Responsibilities

- `__init__.py`: Top-level package exports for `proxbox_api`.
- `app/`: Application composition, bootstrap, middleware, exception handling, cache routes, root metadata, full-update streaming, and websocket wiring.
- `cache.py`: In-memory cache helper used across API workflows.
- `constants.py`: Shared constants for request and sync behavior.
- `database.py`: SQLModel database configuration and NetBox endpoint persistence.
- `dependencies.py`: FastAPI dependency providers shared by route modules.
- `enum/`: Proxmox and NetBox enum definitions used by schemas and routes.
- `exception.py`: Custom exception types and async exception logging helpers.
- `logger.py`: Logging setup utilities for console and file outputs.
- `main.py`: Re-exports `app` from `proxbox_api.app.factory` and symbols used by tests and legacy callers.
- `netbox_async_bridge.py`: Blocking bridge for async NetBox calls when the event loop is already running.
- `netbox_compat.py`: Compatibility helpers around the NetBox client base class.
- `netbox_rest.py`: REST reconciliation helpers for NetBox object sync flows.
- `netbox_sdk_helpers.py` / `netbox_sdk_sync.py`: NetBox SDK compatibility shims.
- `openapi_custom.py`: FastAPI OpenAPI override and Proxmox generated-schema embedding.
- `proxmox_codegen/`: Proxmox API Viewer crawler and OpenAPI/Pydantic code generation pipeline.
- `proxmox_to_netbox/`: Schema-driven normalization from Proxmox payloads to NetBox payloads.
- `routes/`: FastAPI router packages for admin, NetBox, Proxmox, DCIM, virtualization, and Proxbox plugin helpers.
- `schemas/`: Pydantic request/response models and contracts.
- `services/`: Sync orchestration and reusable helper functions.
- `session/`: NetBox and Proxmox session factories and dependency helpers.
- `types/`: Shared aliases and protocol definitions.
- `utils/`: Streaming, retry, status HTML, structured logging, and websocket helpers.
- `e2e/`: Playwright demo auth helpers, fixtures, and shared e2e test data.
- `templates/`: Jinja2 HTML templates used by the admin route.
- `test_*.py`: Package-level smoke tests that run alongside the repository tests.

## Key Data Flow and Dependencies

- `app.factory.create_app()` builds the FastAPI app and includes routers under `/netbox`, `/proxmox`, `/dcim`, `/virtualization`, and `/extras`; `main.py` imports that `app`.
- `database.py` and `session/netbox.py` provide database-backed NetBox connection material used by most routes and sync services.
- `services/sync` modules and `routes/virtualization/virtual_machines` drive synchronization from Proxmox to NetBox objects.

## Extension Guidance

- Keep route modules thin; move business logic to services and helper modules.
- Add new shared dependencies to `dependencies.py` and reuse `Annotated` dependency aliases.
- Prefer explicit Pydantic schemas in `schemas/` for request and response models.
