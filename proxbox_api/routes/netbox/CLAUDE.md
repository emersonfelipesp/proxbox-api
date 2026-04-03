# proxbox_api/routes/netbox Directory Guide

## Purpose

Endpoints for managing NetBox endpoint records and API diagnostics.

## Current Files

- `__init__.py`: NetBox route handlers for endpoint CRUD, status, OpenAPI, and plugin configuration operations.

## How These Routes Work

- CRUD handlers operate on `NetBoxEndpoint` records via SQLModel sessions.
- Status and OpenAPI handlers use an established NetBox session dependency.
- This package is where NetBox endpoint persistence meets the runtime connection state used by the rest of the app.

## Extension Guidance

- Keep database writes transactional and return clear HTTP exceptions.
- Avoid duplicating session setup logic; use `session/netbox` helpers.
- Keep secrets out of serialized response bodies.
