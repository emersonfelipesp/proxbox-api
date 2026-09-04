# proxbox_api/routes/extras Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/extras/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Status endpoints for NetBox bootstrap support objects.

## Current Files

- `__init__.py`: exposes the latest NetBox bootstrap status.

## How These Routes Work

- `GET /extras/bootstrap-status` exposes the last startup NetBox bootstrap status and warnings stored on `app.state.bootstrap_status`.
- Custom-field creation and reconciliation routes are intentionally absent.

## Extension Guidance

- Keep extras routes minimal and schema-driven.
