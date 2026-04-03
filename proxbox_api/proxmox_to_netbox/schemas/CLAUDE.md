# proxbox_api/proxmox_to_netbox/schemas Directory Guide

## Purpose

Schema-driven parsing modules for Proxmox-to-NetBox normalization. All parsing logic lives here, not in `normalize.py` or route handlers.

## Current Files

- `__init__.py`: re-exports the public parsing helpers.
- `disks.py`: `ProxmoxDiskEntry`, disk size conversion, and Proxmox VM config disk parsing.

## Architecture Rule

**ALL normalization and parsing MUST be done inside Pydantic schemas.**

That means:

- parsing logic such as disk config parsing and size conversions belongs in schema validators and computed fields
- normalization functions should be schema methods or computed properties
- route handlers and `normalize.py` only orchestrate and should not parse raw strings

## Adding New Schema Modules

1. Create a new schema module, for example `schemas/interfaces.py`.
2. Add a Pydantic model with validators and computed fields for parsing.
3. Export the new symbols from `schemas/__init__.py`.
4. Import the schema in `proxmox_to_netbox/models.py` if the model needs to expose computed properties.
5. Update `proxmox_to_netbox/CLAUDE.md` to document the new module.
