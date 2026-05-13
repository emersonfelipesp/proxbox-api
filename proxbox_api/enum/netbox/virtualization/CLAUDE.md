# proxbox_api/enum/netbox/virtualization Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/enum/netbox/virtualization/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Virtualization status enumerations for NetBox schema validation.

## Current Files

- `__init__.py`: virtualization status options used by NetBox schema models.

## How These Enums Are Used

- `proxbox_api.schemas.netbox.virtualization` imports these values for cluster and related virtualization payloads.
- Sync flows use them before sending requests to NetBox so choice-field validation fails early in Python instead of at the API boundary.

## Extension Guidance

- Update enum values carefully because they are sent to external NetBox APIs.
- Keep value names aligned with the upstream model field semantics.
