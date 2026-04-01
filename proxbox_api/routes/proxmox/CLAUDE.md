# proxbox_api/routes/proxmox Directory Guide

## Purpose

Endpoints that expose Proxmox sessions, cluster data, nodes, storage, and generated viewer routes.

## Current Files

- `__init__.py`: Proxmox route handlers for sessions, storage, and VM config.
- `cluster.py`: Proxmox cluster endpoints and cluster response schemas.
- `endpoints.py`: Proxmox endpoint listing and resource helpers.
- `nodes.py`: Proxmox node endpoints and node interface response schemas.
- `runtime_generated.py`: Runtime-generated route registration helpers.
- `viewer_codegen.py`: Runtime endpoints to generate and return Proxmox OpenAPI and Pydantic artifacts.

## Key Data Flow and Dependencies

- Uses `ProxmoxSessionsDep` from `session/proxmox.py` for authenticated Proxmox access.
- Route modules provide typed response schemas and dependency aliases.
- Viewer codegen endpoints delegate generation to `proxbox_api.proxmox_codegen`.

## Extension Guidance

- Keep API wrappers resilient to upstream Proxmox errors and convert them to `ProxboxException`.
- Prefer schema-backed responses for stable client behavior.
