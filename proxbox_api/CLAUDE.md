# proxbox_api Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Core FastAPI package for `proxbox-api`. This package owns application composition, route registration, client session factories, schemas, services, generated artifacts, and shared helpers.

## Package Map

- `app/` — application factory, bootstrap, CORS, exception handlers, cache routes, root metadata, full-update orchestration, and WebSocket handlers. See `app/CLAUDE.md`.
- `routes/` — FastAPI route packages for admin, NetBox, Proxmox, DCIM, virtualization, Proxbox plugin access, and sync helpers. See `routes/CLAUDE.md`.
- `services/` — synchronization workflows and reusable helper logic, including the typed Proxmox helper layer and VM reconciliation seam. See `services/CLAUDE.md`.
- `session/` — NetBox and Proxmox session factories, providers, and dependency aliases. See `session/CLAUDE.md`.
- `schemas/` — Pydantic request and response models for external and internal contracts. See `schemas/CLAUDE.md`.
- `enum/` — Proxmox and NetBox choice values used by schemas and routes. See `enum/CLAUDE.md`.
- `proxmox_codegen/` — crawler and generator pipeline that produces Proxmox contract artifacts. See `proxmox_codegen/CLAUDE.md`.
- `proxmox_to_netbox/` — schema-driven transformation from Proxmox payloads to NetBox payloads. See `proxmox_to_netbox/CLAUDE.md`.
- `generated/` — checked-in generated OpenAPI, model artifacts, and runtime route cache data. See `generated/CLAUDE.md`.
- `types/` — shared type aliases and protocol definitions. See `types/CLAUDE.md`.
- `utils/` — streaming, retry, logging, error handling, and WebSocket helper utilities. See `utils/CLAUDE.md`.
- `e2e/` — browser-backed test helpers and fixtures. See `e2e/CLAUDE.md`.
- `custom_objects/` — reserved area for custom NetBox object wrappers. See `custom_objects/CLAUDE.md`.
- `diode/` — experimental Diode sandbox integration. See `diode/CLAUDE.md`.
- `testing/` — test helper utilities including the Proxmox mock fixture (`proxmox_mock.py`).
- `templates/` — Jinja2 templates used by the admin route.
- `static/` — static assets bundled with the package.
- `test_*.py` — package-level smoke tests that run with the repository test suite.

## Runtime Boundaries

- `proxbox_api.app.factory.create_app()` is the application assembly point. It initializes bootstrap state, registers middleware (including `APIKeyAuthMiddleware`), mounts root/cache/full-update/WebSocket routes, and exposes the `app` object imported by `proxbox_api.main`.
- `auth.py` implements bcrypt-hashed API key validation, IP-based brute-force lockout, and the `check_auth_header_with_session` helper used by `APIKeyAuthMiddleware`.
- `database.py` persists NetBox and Proxmox endpoint records, API keys, and auth lockout state in SQLite.
- `session/netbox.py` and `session/proxmox.py` own client construction and dependency wiring. Route handlers should use these dependencies instead of creating clients inline.
- `services/sync/`, `services/sync/reconciliation/`, and
  `routes/virtualization/virtual_machines/` handle the main Proxmox-to-NetBox
  sync flow, including VM operation-queue classification, per-object journal
  tracking, and stream progress.
- `proxmox_to_netbox/` is the normalization boundary. Parsing and conversion must happen in schemas and mappers, not in route handlers.

## Key Data Flow

1. Startup bootstraps the local database and default NetBox session unless bootstrap is skipped.
2. Routes resolve NetBox or Proxmox clients through dependency aliases.
3. Service modules fetch source data, normalize it through schemas, and create or update NetBox objects.
4. Full VM sync prepares Proxmox VM state plus a NetBox snapshot, then calls
   `proxbox_api.services.sync.reconciliation.build_vm_operation_queue()` to
   classify `CREATE`, `GET`, and `UPDATE` operations before dispatch.
5. Sync write sites additively mirror selected legacy custom-field state into
   netbox-proxbox typed sync-state sidecars through
   `services/sync/sync_state_writer.py`. The sidecar payloads come from the same
   live VM/device/cluster/interface/disk values as the custom-field writes and
   remain best-effort for older plugin builds. Sync reads for VM identity and
   orphan last-run state go through `services/sync/sync_state_reader.py`:
   sidecar first, then legacy `cf_*` fallback when the sidecar row or API is
   absent. Role-ownership snapshots remain legacy-CF-only because the VM
   sidecar model has no role ownership field.
6. Route handlers translate those workflows into HTTP, SSE, or WebSocket responses.
7. Generated Proxmox routes are mounted at lifespan startup and may fail open or fail closed depending on `PROXBOX_STRICT_STARTUP`.

## Extension Guidance

- Keep route modules thin and move reusable logic into services or utility modules.
- Add new request and response models to `schemas/` before wiring route code.
- Keep generated artifacts and contract snapshots out of manual edits unless you are debugging the generator.
- Preserve ASCII-only documentation and source text unless a file already requires otherwise.
- Prefer `ProxboxException` for expected API failures and `logger` for operational messages.
- When adding new sync behavior, keep WebSocket and SSE payload shapes aligned.
- When mirroring custom-field state to netbox-proxbox sidecars, keep the
  sidecar written with the same overwrite flag, and treat sidecar API 404/501 or
  transient failures as non-fatal. The typed sidecars are the DEFAULT source of
  truth: legacy reflection custom fields are deprecated and gated behind the
  `custom_fields_enabled` plugin setting (default `false`). Gate every legacy
  custom-field write/read/reconcile on `custom_fields_enabled()` (via the helpers
  in `services/custom_fields.py`: `custom_fields_enabled`,
  `legacy_custom_fields_payload`, `legacy_custom_field_fallback_query`,
  `warn_legacy_custom_fields`), compose it with the existing
  `overwrite_*_custom_fields` flags, and keep building the in-memory
  `custom_fields` dict so the sidecar derivation is unaffected. Never disable
  sidecar writes when the flag is off.
- When reading state that now has a sync-state sidecar, use
  `sync_state_reader.py`. Reads are sidecar-only by default; the legacy `cf_*`
  fallback runs only when `custom_fields_enabled=true` (and emits a deprecation
  warning). Full custom-field retirement is a later item; do not delete
  custom-field data while the flag exists.
- Keep deterministic reconciliation logic in `services/sync/reconciliation/`.
  Do not re-grow operation-queue diffing inside VM route modules.
- For new runtime tunables, prefer a `ProxboxPluginSettings` field on the
  `netbox-proxbox` side over a fresh `PROXBOX_*` env var. Read it through
  `proxbox_api.runtime_settings.get_int / get_float / get_bool / get_str`, which
  resolves env > plugin settings > default with a 5-minute cache. See
  [top-level `CLAUDE.md` → Environment Variables → Adding a new tunable](../CLAUDE.md)
  for the full policy and `.env` keep-list.
