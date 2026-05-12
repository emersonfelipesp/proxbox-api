# proxbox_api/proxmox_to_netbox/mappers Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Contains domain-specific mapping modules from Proxmox raw objects to NetBox payload dictionaries.

## Current Files

- `__init__.py`: mapper package namespace.
- `virtual_machine.py`: maps Proxmox VM resource and config data into NetBox VM create payloads.
- `interfaces.py`: interface mapping extension point.
- `ipam.py`: IPAM mapping extension point.

## How These Mappers Flow

- Schemas in `proxmox_to_netbox/models.py` produce normalized input values.
- Mapper modules turn those normalized values into NetBox request dictionaries.
- Route and service code should only call the mapper entry points, not duplicate mapping logic.

## Extension Guidance

- Keep mapper functions thin and delegate validation to the Pydantic schemas.
- Preserve deterministic field order.
- Omit unknown data only when the NetBox contract requires it.
- Add mapper-specific tests when a new NetBox object type is introduced.
