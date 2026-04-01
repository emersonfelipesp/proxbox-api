# proxbox_api/proxmox_to_netbox/mappers Directory Guide

## Purpose

Contains domain-specific mapping modules from Proxmox raw objects to NetBox payload dictionaries.

## Current Files

- `__init__.py`: Mapper package namespace.
- `virtual_machine.py`: Maps Proxmox VM resource and config data into NetBox VM create payloads.
- `interfaces.py`: Interface mapping extension point.
- `ipam.py`: IPAM mapping extension point.

## Extension Guidance

- Keep mapper functions thin and delegate validation to the Pydantic schemas in `models.py`.
- Preserve deterministic field order and omit unknown data only when the NetBox contract requires it.
