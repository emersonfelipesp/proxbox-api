# Architecture Overview

`proxbox-api` is organized around FastAPI routing, session dependencies, sync services, and schema layers.

## High-level layers

- API layer: `proxbox_api/main.py`, `proxbox_api/routes/*`
- Session layer: `proxbox_api/session/*`
- Service layer: `proxbox_api/services/sync/*`
- Schema and enum layer: `proxbox_api/schemas/*`, `proxbox_api/enum/*`
- Persistence layer: `proxbox_api/database.py`
- Utility layer: decorators, logging, cache, exceptions

## Runtime components

- FastAPI app with grouped routers:
  - `/netbox`
  - `/proxmox`
  - `/dcim`
  - `/virtualization`
  - `/extras`
- SQLite-backed endpoint configuration.
- NetBox API access via `netbox-sdk` sync proxy.
- Proxmox API access via `proxmoxer` sessions.

## Core data models

### `NetBoxEndpoint`

- Fields: `name`, `ip_address`, `domain`, `port`, `token`, `verify_ssl`
- Includes computed `url` property for NetBox session creation.
- API-level singleton behavior is enforced by create endpoint logic.

### `ProxmoxEndpoint`

- Fields: `name`, `ip_address`, `domain`, `port`, `username`, `password`, `verify_ssl`, `token_name`, `token_value`
- Supports both password and token auth models.

## Startup flow

1. App initializes and attempts database table creation.
2. NetBox endpoint is loaded if present.
3. NetBox session is initialized and stored in compatibility layer.
4. CORS origins are assembled.
5. Routers are mounted.

## OpenAPI extension

`proxbox_api/openapi_custom.py` overrides FastAPI OpenAPI generation and embeds generated Proxmox OpenAPI metadata when available:

- Source file: `proxbox_api/generated/proxmox/openapi.json`
- Extension fields:
  - `info.x-proxmox-generated-openapi`
  - `x-proxmox-generated-openapi`

## Sync lifecycle

- Sync endpoints orchestrate Proxmox discovery and NetBox object creation.
- Journal entries and sync-process records are used for traceability.
- WebSocket endpoints stream long-running sync progress.
