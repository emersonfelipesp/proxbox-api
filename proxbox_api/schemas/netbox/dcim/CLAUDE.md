# proxbox_api/schemas/netbox/dcim Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/schemas/netbox/dcim/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Schemas for NetBox DCIM payloads used by synchronization endpoints.

## Current Files

- `__init__.py`: NetBox DCIM schema models used by API payloads.

## How These Schemas Flow

- DCIM route and service modules import these models to validate outgoing device, interface, VLAN, and IP-related payloads.
- Enum and extras schemas feed nested values into these models so the payloads stay API-safe.

## Extension Guidance

- Update fields when NetBox DCIM models evolve.
- Keep optionality accurate so the models reject only truly invalid payloads.
- Add new nested schema dependencies before adding route logic that uses them.
