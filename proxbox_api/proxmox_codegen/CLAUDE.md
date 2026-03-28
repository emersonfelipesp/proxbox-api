# proxbox_api/proxmox_codegen Directory Guide

## Purpose

Implements end-to-end Proxmox API Viewer to OpenAPI 3.1 to Pydantic v2 schema generation.

## Modules and Responsibilities

- `__init__.py`: Exports the top-level pipeline function.
- `apidoc_parser.py`: Fetches and parses `apidoc.js` tree payload from Proxmox API viewer.
- `crawler.py`: Uses async Playwright workers to recursively traverse API viewer items and capture raw endpoint data in parallel.
- `normalize.py`: Normalizes Proxmox method metadata into OpenAPI-ready operations.
- `openapi_generator.py`: Builds OpenAPI 3.1 schema document from normalized operations.
- `pydantic_generator.py`: Generates Pydantic v2 model source code from OpenAPI output.
- `pipeline.py`: Orchestrates crawling, parsing, merge/fallback, and artifact writing.
- `cli.py`: Offline generator CLI entrypoint.

## Data Flow

1. Collect all API Viewer navigation items in memory from Ext tree store.
2. Crawl endpoints in parallel with async worker pages; click method tabs and `Show RAW` for capture.
2. Parse `apidoc.js` and flatten endpoint tree as deterministic fallback source.
3. Merge viewer captures with parser fallback to avoid missing methods.
4. Normalize and convert to OpenAPI 3.1.
5. Generate Pydantic v2 models and persist artifacts.
6. Emit crawl checkpoint snapshots and retry failed endpoint captures with exponential backoff.

## Extension Guidance

- Preserve deterministic ordering for paths and methods to keep diffs stable.
- Keep Proxmox-specific metadata under `x-proxmox` extensions in OpenAPI.
- Prefer additive schema normalization rather than dropping unknown upstream fields.
- Tune `worker_count` based on runtime limits to balance speed and stability.
- Use `retry_count`/`retry_backoff_seconds` to reduce transient UI automation misses.
- Keep `checkpoint_every` small enough for recoverability and large enough to limit write overhead.
