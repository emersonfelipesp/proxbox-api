# proxbox_api Directory Guide

## Purpose

Core FastAPI package: app bootstrap, shared dependencies, persistence, and helpers.

## Modules and Responsibilities

- `__init__.py`: Top-level package exports for proxbox_api.
- `app/`: Application composition — `factory.create_app()`, `bootstrap`, `exceptions`, `websockets`, `cors`, and other app-level wiring (see root `proxbox-api/CLAUDE.md` for error handling and startup).
- `cache.py`: In-memory cache helper used across API workflows.
- `database.py`: SQLModel database configuration and NetBox endpoint model.
- `dependencies.py`: FastAPI dependency providers shared by route modules.
- `exception.py`: Custom exception types and async exception logging helpers.
- `logger.py`: Logging setup utilities for console and file outputs.
- `main.py`: Re-exports `app` from `proxbox_api.app.factory` and symbols used by tests (e.g. `full_update_sync`, `create_virtual_machines`); ASGI entry for uvicorn.
- `openapi_custom.py`: FastAPI OpenAPI override and Proxmox generated-schema embedding.
- `proxmox_codegen/`: Proxmox API Viewer crawler and OpenAPI/Pydantic code generation pipeline.
- `proxmox_to_netbox/`: Schema-driven normalization from Proxmox payloads to NetBox create payloads.
- `test_main.py`: Basic API smoke tests for the FastAPI root endpoint.
- `utils.py`: Legacy utility helpers for sync status rendering.

## Key Data Flow and Dependencies

- `app.factory.create_app()` builds the FastAPI app and includes routers under /netbox, /proxmox, /dcim, /virtualization, and /extras; `main.py` imports that `app`.
- database.py and session/netbox.py provide database-backed NetBox connection material used by most routes and sync services.
- services/sync modules and routes/virtualization/virtual_machines drive synchronization from Proxmox to NetBox objects.

## Extension Guidance

- Keep route modules thin; move business logic to services and helper modules.
- Add new shared dependencies to dependencies.py and reuse Annotated dependency aliases.
- Prefer explicit Pydantic schemas in schemas/ for request and response models.
