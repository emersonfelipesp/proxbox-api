# proxbox-api Documentation

`proxbox-api` is a FastAPI backend that connects Proxmox infrastructure workflows with NetBox data models and plugin objects.

This documentation covers installation, configuration, architecture, API references, synchronization flows, and contribution guidelines.

## What this service does

- Stores local endpoint bootstrap data in SQLite for NetBox and Proxmox connections.
- Exposes REST APIs for Proxmox and NetBox endpoint management.
- Exposes Proxmox data and sync orchestration endpoints.
- Provides WebSocket and SSE streaming endpoints for real-time sync feedback.
- Includes a generated OpenAPI extension for Proxmox API viewer contracts.

## Main capabilities

- NetBox endpoint bootstrap (singleton endpoint behavior).
- Proxmox endpoint CRUD (multiple endpoints).
- Cluster, node, storage, and VM data collection.
- Virtual machine and backup synchronization toward NetBox.
- Custom field bootstrap under NetBox extras.

## Audience

- Network automation engineers integrating NetBox and Proxmox.
- Backend developers extending sync behavior.
- Operators deploying the API as a standalone service.

## Quick links

- API docs (runtime): `/docs` (Swagger UI), `/redoc`.
- Health/info root endpoint: `/`.
- Project repository: <https://github.com/emersonfelipesp/proxbox-api>

## Language

- Default language: English.
- Optional translation: Brazilian Portuguese (`pt-BR`) using the language switcher.
