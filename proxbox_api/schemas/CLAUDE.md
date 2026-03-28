# proxbox_api/schemas Directory Guide

## Purpose

Top-level Pydantic schema package for plugin and API contracts.

## Modules and Responsibilities

- `__init__.py`: Top-level schema exports and plugin configuration schema.
- `proxmox.py`: Pydantic schemas for Proxmox sessions and resource payloads.

## Key Data Flow and Dependencies

- Subpackages provide domain-specific schemas imported by routes and session modules.

## Extension Guidance

- Keep schema defaults explicit and aligned with upstream NetBox and Proxmox fields.
