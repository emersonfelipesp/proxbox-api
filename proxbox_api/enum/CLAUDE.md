# proxbox_api/enum Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/enum/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Central enum definitions for Proxmox path options and NetBox value constraints.

## Current Modules

- `proxmox.py`: Proxmox API path and mode choices.
- `netbox/`: NetBox-specific enum groups.

## How These Enums Are Used

- Route modules import enums for query and path validation.
- Schema modules use enums to keep outgoing payloads aligned with upstream API choices.
- The values are serialized across REST, SSE, and WebSocket payloads, so the enum contracts should remain stable.

## Extension Guidance

- Add new members in a backward-compatible way.
- Keep names and values stable once they are used in external payloads.
- Use `str` enums whenever the values are sent to clients or upstream APIs.
