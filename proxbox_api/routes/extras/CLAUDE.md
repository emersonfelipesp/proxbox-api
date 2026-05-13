# proxbox_api/routes/extras Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/extras/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

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
