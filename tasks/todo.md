# Proxmox API Viewer Codegen Plan

- [x] Design package structure for Proxmox viewer crawling and conversion pipeline.
- [x] Implement Playwright recursive crawler for API Viewer navigation and Show RAW capture.
- [x] Implement apidoc.js parser and endpoint normalization.
- [x] Implement OpenAPI 3.1 generator.
- [x] Implement Pydantic v2 schema generator.
- [x] Integrate runtime API endpoints and offline CLI/script entrypoint.
- [x] Add tests and run compile/test verification.
- [x] Update CLAUDE documentation for new modules.

## Review

- Done. Async parallel crawl verified, generated artifacts validated, compileall and pytest passed.

## Integration Refactor

- [x] Create `integration-refactor` branch and run baseline checks.
- [x] Add FastAPI custom OpenAPI override and embed generated Proxmox OpenAPI.
- [x] Add `proxmox_to_netbox` package with schema loaders and Pydantic v2 VM normalization.
- [x] Refactor VM sync payload build to use `proxmox_to_netbox` mapper/service helper.
- [x] Add tests for Proxmox-to-NetBox mapping and schema contract resolution.
- [x] Run final compile/test validation and fix regressions.

## MkDocs Material Documentation

- [x] Create `material-for-mkdocs` branch from `main`.
- [x] Add MkDocs Material configuration with English default and optional pt-BR locale.
- [x] Write full English documentation for architecture, install, config, API, sync, troubleshooting, tests, and contribution.
- [x] Add pt-BR translated documentation pages with mirrored structure.
- [x] Add GitHub Actions workflow to build docs and publish to `gh-pages` on push to `main`.
- [x] Run strict docs build and resolve configuration warnings/errors.

## Review

- Completed. Added `mkdocs.yml`, full `docs/` tree in EN + pt-BR, docs workflow at `.github/workflows/docs.yml`, and `requirements-docs.txt`.
- Verified with `mkdocs build --strict` on branch `material-for-mkdocs`.

## NetBox Async Session Fix

- [x] Add async NetBox session accessor for async dependencies.
- [x] Refactor `proxmox_sessions(source="netbox")` to iterate async endpoints directly.
- [x] Add regression test for NetBox-backed Proxmox session loading.
- [x] Run targeted tests and verify endpoint behavior.
- [x] Handle netbox-sdk plugin list-path mismatch via direct NetBox client fallback.
- [x] Add regression test for fallback endpoint retrieval.
- [x] Re-run full tests and endpoint verification.

## Review

- Completed. NetBox-backed Proxmox session loading now uses async iteration and avoids SyncProxy in async dependency path.
- Added fallback to `/api/plugins/proxbox/endpoints/proxmox/` when schema list path is unavailable in netbox-sdk facade.
- Verified with `pytest tests/test_session_and_helpers.py` (13 passed) and full `pytest` (30 passed).
- Runtime endpoint now returns auth/domain errors (HTTP 400) instead of generic internal server error when Proxmox credentials fail.

## Migrate Proxmox Client to proxmox-sdk (v0.0.7)

- [x] Record baseline behavior with targeted session tests.
- [x] Replace `proxmoxer` dependency with `proxmox-sdk` in project metadata.
- [x] Migrate Proxmox session factory in `proxbox_api/session/proxmox.py` and `proxbox_api/session/proxmox_core.py`.
- [x] Replace `proxmoxer` exception imports/usages in Proxmox route modules.
- [x] Update tests/fakes to validate proxmox-sdk-backed session wiring.
- [x] Update docs and route metadata text that still references `proxmoxer`.
- [x] Run lint/compile/test validation and capture results.

## Review

- Completed migration on branch `v0.0.7`: runtime dependency switched from `proxmoxer` to `proxmox-sdk`.
- Added compatibility adapter and safe session finalization for request-scoped Proxmox dependencies.
- Updated Proxmox route exception imports and docs/CLAUDE references.
- Validation results:
	- `uv run pytest tests/test_session_and_helpers.py` passed (38/38).
	- `uv run ruff check .` passed.
	- `uv run python -m compileall proxbox_api tests` passed.
	- `uv run pytest tests` reported one pre-existing unit failure in `tests/test_individual_sync.py::test_sync_backup_individual_reports_updated_when_backup_exists` and three env-dependent image HTTP E2E errors requiring `PROXBOX_IMAGE_E2E_BASE_URL`.

## Full Sync Reconciliation Refactor

- [x] Review current full-update VM sync execution order and identify write points.
- [x] Implement in-memory Proxmox data collection for VM sync (parallel fetch with asyncio gather).
- [x] Implement in-memory NetBox VM snapshot loading before reconciliation.
- [x] Add Pydantic-based reconciliation to classify objects as ok/create/update.
- [x] Queue NetBox operations (GET/CREATE/UPDATE) in deterministic sequential order.
- [x] Dispatch queued NetBox operations sequentially in batches using global write concurrency as batch size.
- [x] Integrate the new planner/dispatcher flow into `/virtualization/virtual-machines/create` used by full-update.
- [x] Add/adjust tests for operation classification and sequential dispatch semantics.
- [x] Run targeted pytest validation for VM sync paths.

## Review

- Implemented queue-based in-memory VM reconciliation path for full-update mode (`sync_vm_network=False`) with explicit GET/CREATE/UPDATE operation planning.
- Added deterministic sequential NetBox dispatch in batch windows governed by `PROXBOX_NETBOX_WRITE_CONCURRENCY`.
- Preserved existing network/interface VM sync flow for non-full-update route calls.
- Validation:
	- `uv run pytest tests/test_vm_sync_reconciliation_queue.py` passed (2/2).
	- `uv run pytest tests/test_qemu_guest_agent_sync.py` passed (12/12).
	- `uv run python -m compileall proxbox_api/routes/virtualization/virtual_machines/sync_vm.py tests/test_vm_sync_reconciliation_queue.py` passed.

## E2E NetBox/Proxbox Transport Matrix

- [x] Replace E2E install-source matrix with transport matrix that validates protocol-crossed scenarios.
- [x] Add NetBox transport modes: `http_manage`, `https_nginx`, and `https_granian`.
- [x] Add runner CA trust installation for NetBox HTTPS modes and keep TLS verification enabled.
- [x] Add Proxbox backend transport modes: `http_raw`, `https_nginx`, and `https_granian` as matrix combinations.
- [x] Add explicit E2E preflight check where Proxbox reaches NetBox using the matrix-selected protocol and `verify_ssl` policy.

## Review

- Updated `.github/workflows/ci.yml` E2E job to run transport-focused matrix coverage instead of install-source variants.
- NetBox HTTPS is validated via both nginx TLS termination and granian TLS-serving modes with runner trust store updated from generated CA.
- Proxbox backend is now exercised in both HTTP and HTTPS image variants while still running repository E2E pytest suite against the selected NetBox transport URL.

## E2E Matrix Expansion (HTTPS↔HTTPS + HTTP↔HTTP)

- [x] Add `NB:https_granian` + `PB:https_granian` transport entry.
- [x] Ensure HTTPS↔HTTPS uses granian on both NetBox and Proxbox sides.
- [x] Add `NB:http_manage` + `PB:http_raw` transport entry.

## Review

- Expanded E2E matrix in `.github/workflows/ci.yml` from 4 to 6 transport combinations.
- Added explicit secure symmetric run (`https_granian`/`https_granian`) and insecure symmetric run (`http_manage`/`http_raw`).

## E2E Transport Stabilization (Round 2)

- [x] Fix Proxbox nginx HTTPS template to avoid invalid `map` directive placement in `conf.d` include context.
- [x] Fix NetBox granian startup import target in CI by using a valid NetBox module path and matching granian interface.
- [x] Re-run CI matrix and validate previously failing jobs now pass readiness/startup.

## E2E Transport Stabilization (Round 3)

- [x] Fix NetBox TLS certificates in CI to include CA/server key-usage extensions required by Python SSL verification.
- [x] Fix NetBox HTTPS endpoint host for proxbox-to-netbox checks to use network-reachable container DNS (`netbox-e2e-nginx`) instead of runner localhost.
- [x] Make preflight `/netbox/status` validation accept both `{"available": true}` and direct NetBox status payloads returned by current proxbox API route.
- [x] Fix Proxbox nginx entrypoint to write generated config into Alpine nginx include path (`/etc/nginx/http.d/`).
- [x] Re-run CI and confirm all 6 transport combinations reach E2E pytest stage.

## E2E Transport Stabilization (Round 4)

- [x] Fix Proxbox nginx TLS template conflict with Alpine default SSL session cache zone.
- [x] Force E2E suite to use NetBox token v1 against CI-generated legacy tokens.
- [x] Add E2E token-version env propagation into NetBox E2E session helper.
- [x] Re-run CI and confirm transport matrix advances past E2E setup failures.

## E2E Transport Stabilization (Round 5)

- [x] Fix E2E NetBox SDK session config to pass token via `token_secret` (compatible with current netbox-sdk `Config`).
- [x] Re-run CI and verify E2E tests progress beyond NetBox tag setup.

## E2E Transport Stabilization (Round 6)

- [x] Fix E2E tag fixture to handle `RestRecord` returned by `ensure_tag_async` (serialize/object-attribute fallback).
- [x] Re-run CI and verify E2E suite exits setup and executes test bodies.

## E2E Transport Stabilization (Round 7)

- [x] Fix E2E backup test payload mapping from `storage` to `proxmox_storage` for `NetBoxBackupSyncState` schema compatibility.
- [x] Fix NetBox REST global semaphore to be event-loop-aware and avoid cross-loop binding failures in async E2E tests.
- [x] Re-run CI and verify reduced E2E failures across all 6 transport combinations.

## E2E Transport Stabilization (Round 8)

- [x] Fix remaining backup test payload/normalizer keys in `test_sync_vm_backups_with_e2e_tag` to use `proxmox_storage` consistently.
- [x] Re-run CI and verify backup E2E failures are resolved.

## E2E Transport Stabilization (Round 9)

- [x] Fix backup E2E subtype payloads to use NetBox choice-compatible values (`qemu`/`lxc`) instead of unsupported `private`.

## E2E Transport Stabilization (Round 10)

- [x] Align backup sync payload with plugin model by including legacy `storage` alongside `proxmox_storage`.

## E2E Transport Stabilization (Round 11)

- [x] Fix backup sync payload incompatibility by sending plugin-accepted `storage` (string) field and avoiding `proxmox_storage` write path that crashes in plugin serializer.
- [ ] Re-run CI matrix and verify all 6 transport combinations pass E2E backup tests.
