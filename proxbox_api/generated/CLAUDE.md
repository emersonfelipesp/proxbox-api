# proxbox_api/generated Directory Guide

## Purpose

Holds generated code and schema artifacts produced by build-time and runtime generators.

## Current Modules

- `__init__.py`: package marker for generated artifacts.
- `netbox/`: cached NetBox OpenAPI schema documents and related artifacts.
- `proxmox/`: Proxmox API viewer outputs, including `openapi.json`, `pydantic_models.py`, and crawl caches.

## How This Directory Is Used

- `proxbox_api.proxmox_codegen` writes the Proxmox artifacts here.
- `proxbox_api.proxmox_to_netbox` reads the generated contract snapshots as part of schema validation.
- Runtime route registration can also write cache files here when generated Proxmox routes are rebuilt.

## Extension Guidance

- Treat these files as build outputs.
- Regenerate them instead of editing them by hand.
- Keep versioned snapshots and the `latest/` snapshot in sync with the generator behavior.
