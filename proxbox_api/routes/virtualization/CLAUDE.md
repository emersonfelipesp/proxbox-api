# proxbox_api/routes/virtualization Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/virtualization/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Virtualization route namespace and high-level endpoints.

## Current Files

- `__init__.py`: virtualization route namespace. The `cluster-types/create` and `clusters/create` endpoints are stubs that return HTTP 501.

## How These Routes Work

- This namespace acts as an entry point for cluster and virtual machine synchronization endpoints.
- The functional VM work lives under `virtual_machines/`; this package mainly owns the higher-level virtualization namespace and placeholders.
- The `/virtualization/virtual-machines` router is mounted separately in the app factory and contains the real VM sync surface.

## Extension Guidance

- Promote TODO placeholders into service-backed handlers as functionality is implemented.
- Keep stubbed endpoints explicit so clients know which paths are not implemented yet.
