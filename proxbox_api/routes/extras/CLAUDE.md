# proxbox_api/routes/extras Directory Guide

## Purpose

Endpoints for NetBox extras resources required by synchronization.

## Current Files

- `__init__.py`: Extras route handlers for NetBox custom field management and related plugin data.

## Key Data Flow and Dependencies

- Creates expected custom fields for VM synchronization metadata.
- Returns dependency aliases consumed by VM sync endpoints.

## Extension Guidance

- Add new custom fields in one place and keep names synchronized with plugin expectations.
