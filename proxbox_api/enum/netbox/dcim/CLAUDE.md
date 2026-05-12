# proxbox_api/enum/netbox/dcim Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/enum/netbox/dcim/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

DCIM-specific status enumerations for NetBox schema validation.

## Current Files

- `__init__.py`: DCIM status options used by NetBox schema models.

## How These Enums Are Used

- `proxbox_api.schemas.netbox.dcim` imports these values to constrain payload fields.
- DCIM sync paths use them to keep device and related object payloads valid before requests are sent.

## Extension Guidance

- Mirror canonical NetBox status values exactly.
- Keep the public enum names stable so schema references do not break.
