# proxbox_api/schemas/netbox/virtualization Directory Guide

## Purpose

Schemas for NetBox virtualization objects like clusters and cluster types.

## Current Files

- `__init__.py`: NetBox virtualization schema models for clusters and types.

## Key Data Flow and Dependencies

- Used by synchronization logic to shape valid virtualization object payloads.

## Extension Guidance

- Mirror NetBox model constraints and status choices to reduce runtime validation errors.
