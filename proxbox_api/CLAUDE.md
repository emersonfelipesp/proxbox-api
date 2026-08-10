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

- `proxbox_api.app.factory.create_app()` is the import-safe application assembly point. It validates auth-lockout policy/trusted-proxy process configuration, registers middleware (including `APIKeyAuthMiddleware`), mounts root/cache/full-update/WebSocket routes, and exposes the `app` object imported by `proxbox_api.main`; database/network bootstrap and default adjacent HMAC-key validation occur only when its lifespan starts.
- `auth.py` is the thin sync/async bcrypt validation adapter used by HTTP and WebSocket auth. It delegates every lockout transition to `services/auth_lockout.py`.
- `services/auth_lockout.py` owns validated credential/source policy, per-bucket plus global verification concurrency, trusted-proxy source normalization, server-keyed HMAC bucket identities, and one durable reservation row per bcrypt admission. Each token has an independent expiry; expired crash rows stop consuming capacity and support exactly-once late finalization for one hour, after which admission or finalization compacts them into a durable aggregate counter. Rejected cohorts coalesce only their credential transition; every consumed rejection advances source abuse. Missing keys allocate no credential rows, IPv6 sources aggregate at `/64`, and each source has bounded distinct credential commitments. Failure rows use independent bounded credential/source partitions, with active reservations committing future slots. Saturated partitions evict only safe expired rows, while one globally serialized, per-source lane preserves valid-key verification after unauthenticated row saturation. Credential isolation applies until the deliberately higher shared source-abuse threshold is exhausted. The service also exposes label-free capacity/row/in-flight/orphan/compaction metrics and safe local selectors.
- `auth_lockout_cli.py` requires an explicit existing database for local `list`, `clear`, and identity-key recovery operations. `list` opens SQLite read-only, no command initializes/migrates schema, output is limited to source context and documented 12-character identifiers, and `rebind-key` requires an existing private replacement file plus an exclusive offline runtime lease. Recovery requires all current bucket/reservation/metric/key-binding tables and columns, atomically clears incompatible buckets and reservations, and advances the binding generation (or recreates generation 1 when a lost binding is being recovered).
- `database.py` owns the typed SQLite resolver, fail-closed legacy candidate/auth-history guard, exact/audited one-start override plus durable consumption marker, persistent startup advisory lock, fatal WAL/write probe and migration inspection, lifecycle-managed engines, endpoint/API-key records, bounded auth-lockout tables, HMAC-key generation binding, and durable aggregate counters. The versioned lockout schemas leave the legacy IP-only table intact for rollback and validate required columns/types/keys/constraints under `BEGIN IMMEDIATE`. The `.startup.lock` covers the busy-timeout-first WAL probe, engine/table creation, complete bucket/reservation/metric/key-binding validation, and every migration across processes; key validation pins the HMAC material in process memory so post-start source mutation cannot change identities. Each live worker then holds a shared `.runtime.lock` lease so offline identity-key recovery cannot race request handling. Sync and async engines install the busy timeout before inspecting/negotiating WAL. Use typed accessors after lifespan startup; do not hide legacy/stat or inspection failures, accept raw `?` URL delimiters, delete override markers to re-arm bootstrap, or split the serialized boundary.
- `session/netbox.py` and `session/proxmox.py` own client construction and dependency wiring. Route handlers should use these dependencies instead of creating clients inline.
- `services/sync/`, `services/sync/reconciliation/`, and
  `routes/virtualization/virtual_machines/` handle the main Proxmox-to-NetBox
  sync flow, including VM operation-queue classification, per-object journal
  tracking, and stream progress.
- `services/sync/task_history.py` owns node-archive task collection and global
  NetBox reconciliation. VM routes pass successful VM IDs to it once;
  full-update disables the VM-stage default and runs its dedicated aggregate
  once. Keep endpoint-aware identity and degraded-result semantics at this
  service boundary.
- `proxmox_to_netbox/` is the normalization boundary. Parsing and conversion must happen in schemas and mappers, not in route handlers.

## Key Data Flow

1. Lifespan startup resolves one guarded absolute SQLite target, acquires its persistent sibling lock, verifies WAL/write capability, creates tables/runs all migrations, and then bootstraps the default NetBox session unless skipped. Database verification and the complete interprocess lock boundary are never skipped.
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
- Task-history identity is stricter than a best-effort display read: load the VM
  identity sidecars once, join by `virtual_machine`, and treat each present row
  as authoritative. Never use custom fields to mask a malformed, incomplete, or
  duplicate present sidecar. When custom fields are disabled, inability to
  verify sidecar identity is a fatal sync boundary.
- Keep deterministic reconciliation logic in `services/sync/reconciliation/`.
  Do not re-grow operation-queue diffing inside VM route modules.
- For new runtime tunables, prefer a `ProxboxPluginSettings` field on the
  `netbox-proxbox` side over a fresh `PROXBOX_*` env var. Read it through
  `proxbox_api.runtime_settings.get_int / get_float / get_bool / get_str`, which
  resolves env > plugin settings > default with a 5-minute cache. See
  [top-level `CLAUDE.md` → Environment Variables → Adding a new tunable](../CLAUDE.md)
  for the full policy and `.env` keep-list.
