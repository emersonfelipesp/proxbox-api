# proxbox_api/enum/netbox Directory Guide

## Purpose

Namespace package for NetBox-oriented enum groups.

## Current Modules

- `__init__.py`: Enum namespace for NetBox-related choices.
- `dcim/`: DCIM status and choice enums.
- `virtualization/`: Virtualization cluster status enums.

## Key Data Flow and Dependencies

- Subpackages provide enum values consumed by schemas.

## Extension Guidance

- Keep package init light and re-export only stable enum symbols if needed.
