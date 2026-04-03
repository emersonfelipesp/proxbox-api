# proxbox_api/schemas Directory Guide

## Purpose

Top-level Pydantic schema package for plugin and API contracts.

## Current Modules

- `__init__.py`: top-level schema exports and plugin configuration schema.
- `_base.py`: shared Proxbox base model.
- `proxmox.py`: Pydantic schemas for Proxmox sessions and resource payloads.
- `netbox/`: NetBox session and payload schemas.
- `virtualization/`: VM config and summary schemas.

## How These Schemas Flow

- Route modules consume these schemas directly for request validation and response models.
- Session modules use them for connection and configuration payloads.
- Sync services rely on them as the contract boundary before any data is handed to NetBox or Proxmox clients.

## Extension Guidance

- Keep schema defaults explicit.
- Match upstream NetBox and Proxmox fields carefully so validation fails early and predictably.
- Put parsing and normalization in schema validators or computed fields rather than in route handlers.
