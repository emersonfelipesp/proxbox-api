# proxbox_api/routes/netbox Directory Guide

## Purpose

Endpoints for managing NetBox endpoint records and API diagnostics.

## Current Files

- `__init__.py`: NetBox route handlers for endpoint CRUD, status, OpenAPI, and plugin configuration operations.

## Key Data Flow and Dependencies

- CRUD handlers operate on `NetBoxEndpoint` records via SQLModel sessions.
- Status and OpenAPI handlers use an established NetBox session dependency.

## Extension Guidance

- Keep database writes transactional and return clear HTTP exceptions.
- Avoid duplicating session setup logic; use `session/netbox` helpers.
