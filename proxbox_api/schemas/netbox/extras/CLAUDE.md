# proxbox_api/schemas/netbox/extras Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/schemas/netbox/extras/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Schemas for NetBox extras payloads such as tags and other reusable nested extras objects.

## Current Files

- `__init__.py`: NetBox extras schema models such as tags.

## How These Schemas Flow

- DCIM and virtualization schema packages import these models for typed nested objects.
- Sync services reuse them when they need consistent metadata or tag payloads across multiple domains.

## Extension Guidance

- Keep extras schemas generic and reusable across multiple domains.
- Avoid coupling them to one sync route or object type.
