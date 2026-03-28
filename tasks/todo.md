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
