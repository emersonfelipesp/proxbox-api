# proxbox_api/enum Directory Guide

## Purpose

Central enum definitions for Proxmox path options and API value constraints.

## Modules and Responsibilities

- `proxmox.py`: Enum definitions for Proxmox API path and mode choices.

## Key Data Flow and Dependencies

- Route and schema modules import enums from proxmox.py to validate query and path values.

## Extension Guidance

- Add new enum members in a backward-compatible way and keep names stable.
- Use str Enum where values are serialized in API responses.
