# proxbox_api/proxmox_codegen Directory Guide

## Workspace Context

This file lives at `<repository-root>/proxbox_api/proxmox_codegen/CLAUDE.md` inside the `personal-context` workspace.
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
6. Build runtime Pydantic models directly from parsed OpenAPI data without
   evaluating generated source.
7. Render optional Pydantic source artifacts with validated identifiers and
   literal-safe field metadata.
8. Validate the complete document against fixed byte, schema-depth, path,
   operation, property, model, and string limits before rendering or writing.
9. Persist official-source artifacts only after validating the version tag and
   proving each resolved path remains inside the configured output directory.
   Write `provenance.json` last with the source URL, generation time, and the
   SHA-256 digest of the exact `openapi.json` bytes.
10. Persist non-default-source artifacts under `custom/<version_tag>/` for
    inspection only. Runtime route discovery never reads this namespace.
11. Store crawl checkpoints so interrupted runs can resume.

Runtime HTTP generation and user-schema discovery require the development-only
`PROXBOX_RUNTIME_CODEGEN_ENABLED=true` process setting. Production uses bundled
schemas only. Treat `provenance.json` as corruption detection, not
authentication: a same-UID writer can forge both the artifact and digest.
`proxbox-schema list` and `status` also default to bundled-only discovery. Their
explicit `--include-user` flag is rejected unless the development opt-in is
active, and their output distinguishes user-generated artifacts from bundled
ones.

Bundled OpenAPI loading keeps a bounded process-local LRU of validated
documents keyed by resolved artifact path and raw SHA-256. Each worker performs
the complete validation once for an immutable bundle identity; a replacement
artifact has a different digest and is parsed and validated again. Validation
receipts carry the canonical document SHA-256 and byte count through aggregate
registration and model construction so those stages do not repeat the bounded
tree walk. Every receipt consumer serializes the current object once, verifies
those canonical bytes against the receipt, and parses the same bytes into a
private snapshot. Validation-limit accounting, model and route construction,
cache keys, and route-cache persistence use only that snapshot, so a mutable
in-process caller cannot change the consumed document after verification.
User-generated and explicitly supplied documents are snapshotted before their
complete first-sight validation.

Runtime Pydantic modules are cached per process by `(version tag, canonical
document SHA-256)` in a bounded `MAX_ELIGIBLE_VERSIONS + 2` LRU. Runtime route
plans have a separate two-entry process-local LRU so repeated FastAPI
application instances can reuse already constructed route templates. Each
application receives shallow route clones bound to its own dependency-override
provider, and neither cache crosses a process boundary. Model construction remains eager on the first
registration for each version and digest because FastAPI requires concrete
request and response models when it constructs validation fields and OpenAPI
metadata; deferring it until the first request would change those contracts.
The persisted route cache is serialized deterministically and is rewritten,
along with its provenance, only when the verified existing file has a different
SHA-256.

## Extension Guidance

- Preserve deterministic ordering for paths and methods so diffs stay stable.
- Keep Proxmox-specific metadata under `x-proxmox` extensions in OpenAPI.
- Prefer additive normalization rather than dropping unknown upstream fields.
- Tune `worker_count`, retry counts, and checkpoint frequency to match the environment you are running in.
- Keep runtime model construction on `pydantic.create_model`; generated Python
  source is an offline artifact and must never become a runtime loading path.
- Reject normalized field-name collisions, reserved Pydantic field names, and
  duplicate operation-derived model names before constructing any model.
- Keep type resolution iterative or explicitly depth-bounded. Limit failures
  must raise `SchemaLimitError`, never an interpreter recursion failure.
- Validate every consumed OpenAPI container and scalar before model
  construction. Enforce both per-document and aggregate runtime registration
  limits before building any model or route cache.
- Keep version tags within `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, reject `.` and
  `..`, and validate before starting Playwright or writing any file.
- Bundled tags are immutable. A persisted request must select a new tag, and a
  custom source URL never produces a route-registerable artifact.
