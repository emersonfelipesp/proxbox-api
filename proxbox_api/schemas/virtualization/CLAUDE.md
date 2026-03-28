# proxbox_api/schemas/virtualization Directory Guide

## Purpose

Schemas modeling Proxmox VM config and aggregated VM summaries.

## Modules and Responsibilities

- `__init__.py`: Virtualization schema models and VM configuration validator.

## Key Data Flow and Dependencies

- VMConfig supports dynamic keys from Proxmox config responses.
- Summary models support API response examples and future reporting endpoints.

## Extension Guidance

- Extend dynamic key validation carefully to cover additional Proxmox key prefixes.
- Keep summary schema fields stable for frontend consumers.
