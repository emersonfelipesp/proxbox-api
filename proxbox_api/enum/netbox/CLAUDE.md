# proxbox_api/enum/netbox Directory Guide

## Purpose

Namespace package for NetBox-oriented enum groups.

## Modules and Responsibilities

- `__init__.py`: Enum namespace for NetBox-related choices.

## Key Data Flow and Dependencies

- Subpackages provide DCIM and virtualization status enumerations consumed by schemas.

## Extension Guidance

- Keep package init light and re-export only stable enum symbols if needed.
