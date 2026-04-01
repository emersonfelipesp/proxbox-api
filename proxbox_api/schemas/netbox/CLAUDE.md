# proxbox_api/schemas/netbox Directory Guide

## Purpose

Schemas representing NetBox connection and configuration data.

## Current Modules

- `__init__.py`: Schemas for NetBox session settings and connection details.
- `dcim/`: NetBox DCIM payload schemas.
- `extras/`: NetBox extras payload schemas such as tags.
- `virtualization/`: NetBox virtualization payload schemas.

## Key Data Flow and Dependencies

- Used by plugin configuration routes and dependency setup routines.

## Extension Guidance

- Avoid embedding runtime logic in schemas; keep them declarative.
