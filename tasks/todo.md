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

## Migrate Proxmox Client to proxmox-openapi (v0.0.7)

- [x] Record baseline behavior with targeted session tests.
- [x] Replace `proxmoxer` dependency with `proxmox-openapi` in project metadata.
- [x] Migrate Proxmox session factory in `proxbox_api/session/proxmox.py` and `proxbox_api/session/proxmox_core.py`.
- [x] Replace `proxmoxer` exception imports/usages in Proxmox route modules.
- [x] Update tests/fakes to validate proxmox-openapi-backed session wiring.
- [x] Update docs and route metadata text that still references `proxmoxer`.
- [x] Run lint/compile/test validation and capture results.

## Review

- Completed migration on branch `v0.0.7`: runtime dependency switched from `proxmoxer` to `proxmox-openapi`.
- Added compatibility adapter and safe session finalization for request-scoped Proxmox dependencies.
- Updated Proxmox route exception imports and docs/CLAUDE references.
- Validation results:
	- `uv run pytest tests/test_session_and_helpers.py` passed (38/38).
	- `uv run ruff check .` passed.
	- `uv run python -m compileall proxbox_api tests` passed.
	- `uv run pytest tests` reported one pre-existing unit failure in `tests/test_individual_sync.py::test_sync_backup_individual_reports_updated_when_backup_exists` and three env-dependent image HTTP E2E errors requiring `PROXBOX_IMAGE_E2E_BASE_URL`.
