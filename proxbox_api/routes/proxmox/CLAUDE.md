# proxbox_api/routes/proxmox Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/proxmox/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Endpoints that expose Proxmox sessions, cluster data, node data, viewer generation, and generated live routes.

## Current Files

- `__init__.py`: Proxmox route handlers for sessions, storage, top-level resource access, and typed VM config helpers.
- `cluster.py`: Proxmox cluster endpoints and cluster response schemas.
- `endpoints.py`: Proxmox endpoint CRUD handlers.
- `nodes.py`: Proxmox node endpoints and node interface response schemas.
- `replication.py`: Proxmox cluster replication endpoints.
- `runtime_generated.py`: runtime-generated route registration helpers and cache management.
- `viewer_codegen.py`: runtime endpoints to generate and return Proxmox OpenAPI, Pydantic, and live-route artifacts.

## How These Routes Work

- The package uses `ProxmoxSessionsDep` from `session/proxmox.py` for authenticated access.
- Route modules expose typed response schemas and dependency aliases for client-facing API calls.
- Viewer codegen endpoints delegate generation to `proxbox_api.proxmox_codegen`.
- Runtime-generated routes are mounted during application lifespan and also cached to disk so they can be restored on reload.
- Generated routes are served under `/proxmox/api2/{version_tag}` with `/proxmox/api2/*` kept as the `latest` alias.

## Multi-endpoint dedup (issue #563)

`cluster.py::cluster_resources` deduplicates resources **per cluster identity**
(`px.name`), never globally. Multiple sessions that are nodes of the *same*
cluster each return the full resource list, so same-cluster duplicates are
collapsed; but two *separate* clusters can legitimately share a VMID
(`qemu/100`), so a single global `seen` set would silently drop the second
cluster's resource. Keep the dedup set keyed by cluster identity.

`cluster_status` / `get_node` honor the `proxmox_sessions` selector
(`proxmox_endpoint_ids` / `name` / `domain` / `ip_address`), so the
netbox-proxbox plugin can scope a read to one endpoint and receive only that
endpoint's record(s).

## Extension Guidance

- Keep API wrappers resilient to upstream Proxmox errors and convert them to `ProxboxException`.
- Prefer schema-backed responses for stable client behavior.
- Keep runtime route registration and code generation responsibilities separated.
