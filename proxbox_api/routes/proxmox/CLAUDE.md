# proxbox_api/routes/proxmox Directory Guide

## Purpose

Endpoints that expose Proxmox sessions, cluster data, nodes, storage, and VM config.

## Modules and Responsibilities

- `__init__.py`: Proxmox route handlers for sessions, storage, and VM config.
- `cluster.py`: Proxmox cluster endpoints and cluster response schemas.
- `nodes.py`: Proxmox node endpoints and node interface response schemas.

## Key Data Flow and Dependencies

- Uses ProxmoxSessionsDep from session/proxmox.py for authenticated Proxmox access.
- cluster.py and nodes.py provide typed response schemas and dependency aliases.
- VM and storage helpers are consumed by virtualization sync endpoints.

## Extension Guidance

- Keep API wrappers resilient to upstream Proxmox errors and convert them to ProxboxException.
- Prefer schema-backed responses for stable client behavior.
