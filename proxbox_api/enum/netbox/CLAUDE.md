# proxbox_api/enum/netbox Directory Guide

## Purpose

Namespace package for NetBox-oriented enum groups.

## Current Modules

- `__init__.py`: package marker and export location for stable enum symbols.
- `dcim/`: DCIM status and choice enums.
- `virtualization/`: virtualization cluster status enums.

## How These Enums Are Used

- NetBox schema modules import these enums to constrain payload fields.
- Sync services rely on them to avoid sending invalid choice values to NetBox.

## Extension Guidance

- Keep the package init light.
- Re-export only symbols that are intended to be stable.
- Mirror upstream NetBox values exactly when the enum maps to an external choice field.
