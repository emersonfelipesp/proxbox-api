# proxbox_api/services Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/services/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Reusable business workflows for synchronization, reconciliation, and Proxmox helper logic.

## Current Modules

- `__init__.py`: service package namespace.
- `proxmox_helpers.py`: typed Proxmox helper functions used by route orchestration and validated against generated models.
- `sync/`: main synchronization workflows for clusters, devices, virtual machines, storage, backups, snapshots, disks, interfaces, IPs, and task history.
- `sync/individual/`: targeted single-object sync workflows with dependency auto-creation and dry-run support.

## How Services Are Used

- Route handlers import these modules to keep HTTP, SSE, and WebSocket code thin.
- `session/` provides the authenticated clients that service functions consume.
- `schemas/` and `proxmox_to_netbox/` provide the normalization layer that services rely on.

## Extension Guidance

- Keep service functions independent from request objects where possible.
- Prefer idempotent operations so repeated sync runs are safe.
- Surface predictable errors through `ProxboxException`.
- Keep response payloads compatible with both JSON and stream transports when a service is reused in SSE or WebSocket paths.
