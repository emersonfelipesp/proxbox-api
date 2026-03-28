# proxbox_api/routes/extras Directory Guide

## Purpose

Endpoints for NetBox extras resources required by synchronization.

## Modules and Responsibilities

- `__init__.py`: Extras route handlers for NetBox custom field management.

## Key Data Flow and Dependencies

- create_custom_fields creates expected custom fields for VM synchronization metadata.
- Returned dependency alias is consumed by VM sync endpoints.

## Extension Guidance

- Add new custom fields in one place and keep names synchronized with plugin expectations.
