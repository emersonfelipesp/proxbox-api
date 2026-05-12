# proxbox_api/proxmox_codegen Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/proxmox_codegen/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Implements the Proxmox API Viewer to OpenAPI 3.1 to Pydantic v2 generation pipeline.

## Modules and Responsibilities

- `__init__.py`: exports the top-level pipeline entry points.
- `apidoc_parser.py`: fetches and parses `apidoc.js` tree payloads from the Proxmox API viewer.
- `crawler.py`: uses async Playwright workers to traverse the viewer and capture raw endpoint data in parallel.
- `models.py`: crawl result and normalized API metadata models.
- `normalize.py`: turns captured method metadata into OpenAPI-ready operations.
- `openapi_generator.py`: builds the OpenAPI 3.1 schema document from normalized operations.
- `pydantic_generator.py`: generates Pydantic v2 model source from OpenAPI output.
- `pipeline.py`: orchestrates crawling, parsing, merge fallback, and artifact writing.
- `validation_generator.py`: builds validation helpers from captured schema data.
- `utils.py`: shared generator utilities and file-writing helpers.
- `cli.py`: offline generator CLI entry point.

## Data Flow

1. Collect the API viewer navigation tree.
2. Crawl endpoints in parallel, open each method tab, and capture `Show RAW` output.
3. Parse `apidoc.js` as a deterministic fallback source.
4. Merge crawl output with parser fallback to avoid missing methods.
5. Normalize the merged data into OpenAPI 3.1.
6. Generate Pydantic models and persist all artifacts.
7. Store crawl checkpoints so interrupted runs can resume.

## Extension Guidance

- Preserve deterministic ordering for paths and methods so diffs stay stable.
- Keep Proxmox-specific metadata under `x-proxmox` extensions in OpenAPI.
- Prefer additive normalization rather than dropping unknown upstream fields.
- Tune `worker_count`, retry counts, and checkpoint frequency to match the environment you are running in.
