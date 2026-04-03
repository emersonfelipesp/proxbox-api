# Architecture Overview

`proxbox-api` is organized around FastAPI routing, session dependencies, sync services, and schema layers.

## High-level Layers

- API layer: `proxbox_api/main.py`, `proxbox_api/app/*`, and `proxbox_api/routes/*`
- Session layer: `proxbox_api/session/*`
- Service layer: `proxbox_api/services/*`
- Schema and enum layer: `proxbox_api/schemas/*`, `proxbox_api/enum/*`
- Persistence layer: `proxbox_api/database.py`
- Utility layer: streaming, logging, cache, retry, and error helpers

## Runtime Components

- FastAPI app mounts the current route groups:
  - `/`
  - `/cache`
  - `/clear-cache`
  - `/full-update`
  - `/ws`
  - `/ws/virtual-machines`
  - `/admin`
  - `/netbox`
  - `/proxmox`
  - `/dcim`
  - `/virtualization`
  - `/extras`
  - `/sync/individual`
- SQLite-backed endpoint configuration and bootstrap state.
- NetBox API access via `netbox-sdk` sync and async clients.
- Proxmox API access via `proxmoxer` sessions and typed helper wrappers.
- Runtime-generated Proxmox live routes mounted during app lifespan startup.

## Core Data Models

### `NetBoxEndpoint`

- Fields: `name`, `ip_address`, `domain`, `port`, `token_version`, `token_key`, `token`, `verify_ssl`
- Supports both NetBox token v1 and v2 shapes.
- Includes computed `url` property for NetBox session creation.
- API-level singleton behavior is enforced by create endpoint logic.

### `ProxmoxEndpoint`

- Fields: `name`, `ip_address`, `domain`, `port`, `username`, `password`, `verify_ssl`, `token_name`, `token_value`
- `domain` is optional and `name` is unique.
- Supports either password auth or token-pair auth.

## Startup Flow

1. `create_app()` initializes the database and NetBox bootstrap state.
2. The app mounts static assets, CORS middleware, exception handlers, cache routes, full-update routes, and WebSocket routes.
3. Route packages are included for NetBox, Proxmox, DCIM, virtualization, extras, and individual sync helpers.
4. Generated Proxmox live routes are mounted during lifespan startup and can fail open unless `PROXBOX_STRICT_STARTUP` is enabled.
5. The custom OpenAPI builder embeds the generated Proxmox OpenAPI contract when one is available.

## OpenAPI Extension

`proxbox_api/openapi_custom.py` overrides FastAPI OpenAPI generation and embeds generated Proxmox OpenAPI metadata when available:

- Source file: `proxbox_api/generated/proxmox/latest/openapi.json`
- Extension fields:
  - `info.x-proxmox-generated-openapi`
  - `x-proxmox-generated-openapi`

## Sync Lifecycle

- Sync endpoints orchestrate Proxmox discovery and NetBox object creation.
- Journal entries and sync-process records are used for traceability.
- WebSocket and SSE streaming endpoints provide real-time sync progress with per-object granularity.
