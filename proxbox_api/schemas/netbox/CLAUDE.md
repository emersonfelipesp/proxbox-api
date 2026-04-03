# proxbox_api/schemas/netbox Directory Guide

## Purpose

Schemas representing NetBox connection and configuration data.

## Current Modules

- `__init__.py`: schemas for NetBox session settings and connection details.
- `dcim/`: NetBox DCIM payload schemas.
- `extras/`: NetBox extras payload schemas such as tags and custom metadata.
- `virtualization/`: NetBox virtualization payload schemas.

## How These Schemas Flow

- Plugin configuration routes use these models to validate endpoint records and client settings.
- Sync services use them to shape outgoing NetBox create and update payloads.
- Nested schema packages keep the resource-specific contracts separated by NetBox domain.

## Extension Guidance

- Keep the models declarative and validation-focused.
- Avoid putting request orchestration or network calls in schema modules.
- Mirror upstream NetBox field constraints as closely as possible.
