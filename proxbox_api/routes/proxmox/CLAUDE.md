# proxbox_api/routes/proxmox Directory Guide

## Purpose

Endpoints that expose Proxmox sessions, cluster data, nodes, storage, and generated viewer routes.

## Current Files

- `__init__.py`: Proxmox route handlers for sessions, storage, and VM config.
- `cluster.py`: Proxmox cluster endpoints and cluster response schemas.
- `endpoints.py`: Proxmox endpoint listing and resource helpers.
- `nodes.py`: Proxmox node endpoints and node interface response schemas.
- `runtime_generated.py`: runtime-generated route registration helpers.
- `viewer_codegen.py`: runtime endpoints to generate and return Proxmox OpenAPI and Pydantic artifacts.

## How These Routes Work

- The package uses `ProxmoxSessionsDep` from `session/proxmox.py` for authenticated access.
- Route modules expose typed response schemas and dependency aliases for client-facing API calls.
- Viewer codegen endpoints delegate generation to `proxbox_api.proxmox_codegen`.
- Runtime-generated routes are mounted during application lifespan and should stay aligned with the generated artifact tree.

## Extension Guidance

- Keep API wrappers resilient to upstream Proxmox errors and convert them to `ProxboxException`.
- Prefer schema-backed responses for stable client behavior.
- Keep runtime route registration and code generation responsibilities separated.
