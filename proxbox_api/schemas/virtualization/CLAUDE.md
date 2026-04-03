# proxbox_api/schemas/virtualization Directory Guide

## Purpose

Schemas modeling Proxmox VM config and aggregated VM summaries.

## Current Files

- `__init__.py`: virtualization schema models and VM configuration validator.

## How These Schemas Flow

- `VMConfig` supports dynamic keys from Proxmox config responses and is consumed by VM-related sync routes.
- Summary models support API response examples and future reporting endpoints.

## Extension Guidance

- Extend dynamic key validation carefully so new Proxmox key prefixes are accepted intentionally.
- Keep summary schema fields stable for frontend and API consumers.
- Put parsing and normalization into validators or computed fields rather than into route handlers.
