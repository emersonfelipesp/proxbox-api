# proxbox_api/schemas Directory Guide

## Purpose

Top-level Pydantic schema package for plugin and API contracts.

## Current Modules

- `__init__.py`: Top-level schema exports and plugin configuration schema.
- `_base.py`: Shared Proxbox base model.
- `proxmox.py`: Pydantic schemas for Proxmox sessions and resource payloads.
- `netbox/`: NetBox session and payload schemas.
- `virtualization/`: VM config and summary schemas.

## Key Data Flow and Dependencies

- Subpackages provide domain-specific schemas imported by routes and session modules.

## Extension Guidance

- Keep schema defaults explicit and aligned with upstream NetBox and Proxmox fields.
