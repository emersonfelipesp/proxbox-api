# proxbox_api/routes Directory Guide

## Purpose

Top-level route namespace package for FastAPI router modules.

## Modules and Responsibilities

- `__init__.py`: Route package namespace for proxbox_api endpoints.

## Key Data Flow and Dependencies

- `proxbox_api.app.factory.create_app()` imports routers from nested route packages and mounts them with prefixes.

## Extension Guidance

- Create new endpoint groups as subpackages and register them in `proxbox_api.app.factory.create_app()`.
