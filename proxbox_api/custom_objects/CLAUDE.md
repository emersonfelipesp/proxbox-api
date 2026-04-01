# proxbox_api/custom_objects Directory Guide

## Purpose

Namespace reserved for custom NetBox object wrappers and plugin-specific entity helpers.

## Current Files

- `__init__.py`: Package marker; there are no active Python modules here yet.

## Key Data Flow and Dependencies

- This directory is reserved for future custom object wrappers. The sync audit trail currently uses NetBox journal entries instead.

## Extension Guidance

- Keep schema and API metadata aligned with the NetBox plugin model names.
- Prefer additive schema changes to preserve compatibility with existing records.
