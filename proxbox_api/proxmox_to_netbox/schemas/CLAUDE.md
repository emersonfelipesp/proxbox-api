# proxbox_api/proxmox_to_netbox/schemas Directory Guide

## Purpose

Schema-driven parsing modules for Proxmox-to-NetBox normalization. All parsing logic lives here - not in `normalize.py` or route handlers.

## Modules

- `__init__.py`: Public exports for schema utilities.
- `disks.py`: Proxmox disk entry parsing (ProxmoxDiskEntry schema, size conversion, config parsing).

## Architecture Rule

**ALL normalization and parsing MUST be done inside Pydantic schemas.**

This means:
- Parsing logic (e.g., disk config parsing, size conversions) lives in schema validators and computed fields
- Normalization functions are schema methods or computed properties
- Route handlers and `normalize.py` should ONLY orchestrate; no parsing logic

## Adding New Schema Modules

1. Create a new schema module (e.g., `schemas/interfaces.py`)
2. Add Pydantic model with validators and computed fields for parsing
3. Export from `schemas/__init__.py`
4. Import schema in `models.py` to add computed properties to existing models
5. Update `proxmox_to_netbox/CLAUDE.md` to document the new module
