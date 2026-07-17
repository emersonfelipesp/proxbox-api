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

- `__init__.py`: thin extras route handlers for NetBox custom field reconcile, cache bypass, and bootstrap status.

## How These Routes Work

- These routes create or expose the custom fields and related extras metadata required by VM synchronization.
- `POST /extras/custom-fields/reconcile` is the supported operator recovery route for missing or drifted NetBox custom fields; it bypasses the process-local custom-field cache and clears only `/api/extras/custom-fields/` entries from the lower-level NetBox GET cache before live lookup.
- Custom-field reconcile failures leave the process-local custom-field cache
  empty. A duplicate create response that cannot be re-fetched from NetBox is
  surfaced as a failed field instead of being cached as success.
- `GET /extras/bootstrap-status` exposes the last startup NetBox bootstrap status and warnings stored on `app.state.bootstrap_status`.
- `GET /extras/extras/custom-fields/create` is the legacy double-prefix route and must keep working for older callers.
- They also provide dependency aliases that the VM routes use when constructing sync workflows.

## Extension Guidance

- Add new custom fields only in `proxbox_api/services/custom_fields.py`; startup bootstrap and extras routes consume that same inventory object.
- Keep extras routes minimal and schema-driven.
