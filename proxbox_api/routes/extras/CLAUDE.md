# proxbox_api/routes/extras Directory Guide

## Purpose

Endpoints for NetBox extras resources required by synchronization.

## Current Files

- `__init__.py`: extras route handlers for NetBox custom field management and related plugin data.

## How These Routes Work

- These routes create or expose the custom fields and related extras metadata required by VM synchronization.
- They also provide dependency aliases that the VM routes use when constructing sync workflows.

## Extension Guidance

- Add new custom fields in one place and keep names synchronized with plugin expectations.
- Keep extras routes minimal and schema-driven.
