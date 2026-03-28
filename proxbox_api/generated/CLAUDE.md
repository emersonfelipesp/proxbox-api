# proxbox_api/generated Directory Guide

## Purpose

Holds generated code and schema artifacts produced by build-time and runtime generators.

## Modules and Responsibilities

- `__init__.py`: Package marker for generated artifacts.
- `proxmox/`: Proxmox API viewer generation outputs (`openapi.json`, `pydantic_models.py`, `raw_capture.json`).
- `netbox/`: NetBox OpenAPI cache output (`openapi.json`) fetched from live endpoint when available.

## Extension Guidance

- Treat generated files as build outputs; avoid manual edits unless debugging generation behavior.
