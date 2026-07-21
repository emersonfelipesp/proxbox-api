# proxbox_api/routes/dcim Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/dcim/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Endpoints that synchronize and expose DCIM entities in NetBox.

## Current Files

- `__init__.py`: DCIM route handlers for device discovery, device creation, interface creation, VLAN/IP reconciliation, and SSE stream variants.

## How These Routes Work

- Route handlers consume Proxmox-derived dependencies and sync services to create or update NetBox DCIM objects.
- They depend on NetBox reconciliation helpers and `WebSocketSSEBridge` for streamed progress responses.
- The route layer should stay thin and defer the object-specific workflow to `services/sync`.
- Batch node-interface sync (`/dcim/devices/interfaces/create` and stream)
  fetches live `GET /nodes/{node}/network` data for each node through the
  shared `services.sync.network.load_proxmox_node_network()` helper, resolving
  the Proxmox session per cluster. It maps interfaces to the NetBox
  `dcim.Device` found by exact device name within the owning cluster's NetBox
  site scope, not by name alone and not by the Proxmox cluster-status node id.
  The single-node route accepts the same `cluster_name` query used by the
  Proxmox node-network route so same-name nodes in different clusters/sites are
  resolved to the correct device.

## Extension Guidance

- Keep endpoint orchestration simple.
- Preserve response model declarations so API contracts stay stable.
- Use `WebSocketSSEBridge` and `StreamingResponse` with `text/event-stream` for new stream endpoints.
