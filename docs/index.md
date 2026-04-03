# proxbox-api Documentation

`proxbox-api` is a FastAPI backend that connects Proxmox infrastructure workflows with NetBox data models and plugin objects.

This documentation covers installation, configuration, architecture, API references, synchronization flows, and contribution guidelines.

## What this service does

- Stores local bootstrap data in SQLite for NetBox and Proxmox connections.
- Exposes REST APIs for endpoint management, status checks, and live-generated Proxmox contract routes.
- Exposes Proxmox discovery and sync orchestration endpoints, plus targeted individual-sync helpers.
- Provides WebSocket and SSE streaming endpoints for real-time sync feedback.
- Includes a generated OpenAPI extension for Proxmox API viewer contracts.

## Main capabilities

- NetBox endpoint bootstrap with token v1 and v2 support.
- Proxmox endpoint CRUD with password or token-pair auth.
- Cluster, node, storage, VM, backup, snapshot, and replication data collection.
- Virtual machine, interface, IP, disk, storage, and backup synchronization toward NetBox.
- Admin log inspection, cache inspection, and full-update orchestration.

## Audience

- Network automation engineers integrating NetBox and Proxmox.
- Backend developers extending sync behavior.
- Operators deploying the API as a standalone service.

## Quick links

- API docs (runtime): `/docs` and `/redoc`.
- Root metadata endpoint: `/`.
- Project repository: <https://github.com/emersonfelipesp/proxbox-api>

## Language

- Default language: English.
- Optional translation: Brazilian Portuguese (`pt-BR`) using the language switcher.
