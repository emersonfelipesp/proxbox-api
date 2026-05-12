# proxbox_api/routes/proxbox/clusters Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/proxbox/clusters/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Reserved route namespace for Proxbox cluster-specific endpoints.

## Current Files

- `__init__.py`: cluster-specific Proxbox route namespace.

## Current Role

- This package is currently empty and exists to keep future cluster routes organized.
- No active request handling lives here yet.

## Extension Guidance

- Add endpoints here when introducing plugin-side cluster resource APIs.
- Keep the namespace reserved until there is a real route surface to document.
