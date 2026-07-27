# proxbox_api/schemas Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/schemas/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Top-level Pydantic schema package for plugin and API contracts.

## Current Modules

- `__init__.py`: top-level schema exports and plugin configuration schema.
- `_base.py`: shared Proxbox base model.
- `firecracker.py`: Firecracker host-agent, image bundle, network, micro-VM state, metrics, and Cloud provisioning contracts.
- `proxmox.py`: Pydantic schemas for Proxmox sessions, cluster resources, node payloads, and resource payloads.
- `stream_messages.py`: typed stream event payload schemas used by SSE and WebSocket progress reporting.
- `cloud_provision.py`: Cloud provisioning plus Cloud Image Pipeline contracts,
  including preflight v1 findings/capabilities and the secret-safe build
  response v2. `CloudImageBuildTarget` is the shared provider/storage contract
  for preflight and rendering; it derives snippet requirements and only claims
  image storage for ISO providers. `CloudImageSourceBuildCommand` is the
  fixed-argv source recipe allowlist. Sensitive rendered material is
  nested in the explicit preview model and is valid only for `execute=false`
  requests.
- `cloud_image_security.py`: route-neutral SSH host/user/key/fingerprint
  normalization plus `CloudImageSSHExecutionTarget`, the validated persisted
  execution authority. Keep this module independent of route packages so
  endpoint and cloud schemas can import it without a cycle.
- `zfs.py`: typed ZFS pool summary/detail contracts, recursive vdev tree nodes, and tier-selection metadata for `/proxmox/storage/zfs/*`.
- `netbox/`: NetBox session, endpoint, and payload schemas.
- `virtualization/`: VM config and summary schemas.

## How These Schemas Flow

- Route modules consume these schemas directly for request validation and response models.
- Session modules use them for connection and configuration payloads.
- Sync services rely on them as the contract boundary before any data is handed to NetBox or Proxmox clients.
- Streaming helpers in `proxbox_api/utils/streaming.py` and `proxbox_api/app/full_update.py` use `stream_messages.py` models to keep event shapes stable across transports.

## Extension Guidance

- Keep schema defaults explicit.
- Match upstream NetBox and Proxmox fields carefully so validation fails early and predictably.
- Put parsing and normalization in schema validators or computed fields rather than in route handlers.
- Keep Packer finding objects restricted to `code`, `severity`, `target`, and
  `message`; keep build responses closed with `extra="forbid"` so raw scripts,
  process output, signed URLs, or cloud-init secrets cannot reappear
  accidentally.
- Keep `vm_storage` canonical. Through `0.0.21.x`, accept legacy `storage` only
  as an input alias, reject divergent dual values, and do not emit the alias in
  schema dumps or OpenAPI. Derive provider storage requirements through
  `CloudImageBuildTarget` instead of adding a second authority.
