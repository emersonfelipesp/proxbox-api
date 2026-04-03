# proxbox_api/custom_objects Directory Guide

## Purpose

Reserved namespace for custom NetBox object wrappers and plugin-specific entity helpers.

## Current Files

- `__init__.py`: package marker only; there are no active Python modules here yet.

## Current Role in the App

- The repository currently relies on standard NetBox models and journal entries for sync tracking.
- This package is available for future wrappers when the backend needs a custom abstraction that does not belong in `schemas/` or `services/`.

## Extension Guidance

- Keep wrapper names aligned with NetBox plugin and model names.
- Prefer additive changes so existing payloads and records keep working.
- Add a scoped guide only if this package starts containing real implementation modules.
