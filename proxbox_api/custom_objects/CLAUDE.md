# proxbox_api/custom_objects Directory Guide

## Purpose

Custom object definitions that model plugin-specific NetBox entities.

## Modules and Responsibilities

(No Python modules are currently present in this directory.)

## Key Data Flow and Dependencies

- This directory is reserved for custom NetBox object wrappers. The sync audit trail uses NetBox journal entries instead.

## Extension Guidance

- Keep schema and API metadata aligned with the NetBox plugin model names.
- Prefer additive schema changes to preserve compatibility with existing records.
