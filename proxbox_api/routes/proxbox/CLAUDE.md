# proxbox_api/routes/proxbox Directory Guide

## Purpose

Endpoints exposing Proxbox plugin configuration and settings views.

## Current Files

- `__init__.py`: Proxbox plugin route handlers for configuration access.

## How These Routes Work

- These handlers read plugin configuration from NetBox and map it into local Pydantic schemas.
- They are part of the configuration path that lets the backend discover Proxmox endpoint data.

## Extension Guidance

- Validate external configuration values before returning or using them.
- Keep optional NetBox imports isolated to the handlers that need them.
- Add new plugin-facing routes only when the data belongs to the Proxbox configuration surface.
