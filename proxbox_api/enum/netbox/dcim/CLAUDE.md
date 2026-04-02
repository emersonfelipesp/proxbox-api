# proxbox_api/enum/netbox/dcim Directory Guide

## Purpose

DCIM-specific status enumerations for NetBox schema validation.

## Current Files

- `__init__.py`: DCIM status options used by NetBox schema models.

## Key Data Flow and Dependencies

- `schemas/netbox/dcim` imports `StatusOptions` to constrain site and DCIM object status fields.

## Extension Guidance

- Mirror canonical NetBox status values to avoid API rejection errors.
