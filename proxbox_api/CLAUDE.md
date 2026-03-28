# proxbox_api Directory Guide

## Purpose

Core FastAPI package: app bootstrap, shared dependencies, persistence, and helpers.

## Modules and Responsibilities

- `__init__.py`: Top-level package exports for proxbox_api.
- `cache.py`: In-memory cache helper used across API workflows.
- `database.py`: SQLModel database configuration and NetBox endpoint model.
- `dependencies.py`: FastAPI dependency providers shared by route modules.
- `exception.py`: Custom exception types and async exception logging helpers.
- `logger.py`: Logging setup utilities for console and file outputs.
- `main.py`: FastAPI application entrypoint and route registration.
- `test_main.py`: Basic API smoke tests for the FastAPI root endpoint.
- `utils.py`: Legacy utility helpers for sync status rendering.

## Key Data Flow and Dependencies

- main.py builds the FastAPI app and includes all routers under /netbox, /proxmox, /dcim, /virtualization, and /extras.
- database.py and session/netbox.py provide database-backed NetBox connection material used by most routes and sync services.
- services/sync modules and routes/virtualization/virtual_machines drive synchronization from Proxmox to NetBox objects.

## Extension Guidance

- Keep route modules thin; move business logic to services and helper modules.
- Add new shared dependencies to dependencies.py and reuse Annotated dependency aliases.
- Prefer explicit Pydantic schemas in schemas/ for request and response models.
