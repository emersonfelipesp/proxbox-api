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
