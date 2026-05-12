# proxbox_api/generated/netbox Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/generated/netbox/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Stores cached NetBox OpenAPI schema documents fetched from live NetBox instances.

## Typical Files

- `openapi.json`: last fetched NetBox OpenAPI document used by transformation contracts.
- `__init__.py`: package marker for generated cache artifacts.

## How This Directory Is Used

- `proxbox_api.proxmox_to_netbox.netbox_schema` reads the cached contract when a live schema fetch is unavailable.
- The cached schema acts as a fallback only; live NetBox schema fetches still take precedence when available.
- Regeneration should happen through the normal generator path, not by manual editing.

## Extension Guidance

- Treat files in this directory as generated cache artifacts.
- Keep any fallback contract data aligned with the live schema behavior.
