# proxbox_api/proxmox_to_netbox/mappers Directory Guide

## Purpose

Contains domain-specific mapping modules from Proxmox raw objects to NetBox payload dictionaries.

## Modules and Responsibilities

- `virtual_machine.py`: Maps Proxmox VM resource/config into NetBox VM create payload.
- `interfaces.py`: Placeholder for VM/interface mapping expansion.
- `ipam.py`: Placeholder for IPAM mapping expansion.

## Extension Guidance

- Keep mapper functions thin and delegate validation to `models.py` Pydantic schemas.
