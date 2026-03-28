# proxbox_api/utils Directory Guide

## Purpose

Utility helpers and decorators shared across synchronization workflows.

## Modules and Responsibilities

- `__init__.py`: Utility package exports for decorators and status helpers.
- `sync_decorator.py`: Decorator that tracks sync process lifecycle in NetBox.

## Key Data Flow and Dependencies

- sync_decorator.py wraps sync functions to create and finalize sync process records.
- __init__.py re-exports helper functions consumed by routes and services.

## Extension Guidance

- Keep utility code generic and free from route-specific assumptions.
- When adding decorators, ensure both success and failure paths update sync state.
