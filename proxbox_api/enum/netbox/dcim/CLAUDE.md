# proxbox_api/enum/netbox/dcim Directory Guide

## Purpose

DCIM-specific status enumerations for NetBox schema validation.

## Current Files

- `__init__.py`: DCIM status options used by NetBox schema models.

## How These Enums Are Used

- `proxbox_api.schemas.netbox.dcim` imports these values to constrain payload fields.
- DCIM sync paths use them to keep device and related object payloads valid before requests are sent.

## Extension Guidance

- Mirror canonical NetBox status values exactly.
- Keep the public enum names stable so schema references do not break.
