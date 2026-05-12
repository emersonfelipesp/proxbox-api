# proxbox_api/schemas/virtualization Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/schemas/virtualization/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

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
