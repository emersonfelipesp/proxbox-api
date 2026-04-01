# proxbox_api/generated Directory Guide

## Purpose

Holds generated code and schema artifacts produced by build-time and runtime generators.

## Current Modules

- `__init__.py`: Package marker for generated artifacts.
- `netbox/`: Cached NetBox OpenAPI schema document and related artifacts.
- `proxmox/`: Proxmox API viewer generation outputs, including `openapi.json`, `pydantic_models.py`, and runtime caches.

## Extension Guidance

- Treat generated files as build outputs; avoid manual edits unless debugging generation behavior.
