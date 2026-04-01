# proxbox_api/routes/proxbox Directory Guide

## Purpose

Endpoints exposing Proxbox plugin configuration and settings views.

## Current Files

- `__init__.py`: Proxbox plugin route handlers for configuration access.

## Key Data Flow and Dependencies

- Reads NetBox plugin configuration and maps it into local Pydantic schemas.

## Extension Guidance

- Validate external configuration values before returning or using them.
- Isolate NetBox-specific imports inside handlers when optional at runtime.
