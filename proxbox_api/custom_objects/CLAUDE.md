# proxbox_api/custom_objects Directory Guide

## Purpose

Custom object definitions that model plugin-specific NetBox entities.

## Modules and Responsibilities

- `sync_process.py`: Custom object to manage sync processes.

## Key Data Flow and Dependencies

- sync_process.py defines a NetBoxBase-backed object model used for sync process records.
- Services and decorators reference sync process objects to track run status and metadata.

## Extension Guidance

- Keep schema and API metadata aligned with the NetBox plugin model names.
- Prefer additive schema changes to preserve compatibility with existing records.
