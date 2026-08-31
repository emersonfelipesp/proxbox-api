# tests/ Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/tests/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Unit, integration, and end-to-end tests for the `proxbox_api` backend package. All tests run against the `proxbox_api` package with dependency-injected mocks for NetBox and Proxmox sessions. The `tests/e2e/` subdirectory holds API-level end-to-end tests that wire the full FastAPI app to a `proxmox-sdk` mock (HTTP container or in-process backend) — they use `httpx.AsyncClient`, not Playwright.

## Test File Index

| File | What it tests |
|------|---------------|
| `conftest.py` | Global fixtures: test DB engine, sync TestClient (`test_client`, `auth_test_client`), async client (`authenticated_client`), dependency overrides, fake NetBox session, auth headers |
| `fixtures.py` | Shared reusable fixtures imported by multiple test modules |
| `test_admin_logs.py` | In-memory log buffer routes (`/admin/logs`) |
| `test_api_routes.py` | API route integration tests (request/response contracts) |
| `test_backups_vm_sync.py` | VM backup discovery and sync workflow |
| `test_bridge_interfaces.py` | VM bridge interface mapping and reconciliation |
| `test_bulk_sync_error_accounting.py` | Per-batch error tallies for bulk VM sync paths |
| `test_credentials.py` | Credential encryption/decryption round-trip and Fernet key resolution |
| `test_core_utility_contracts.py` | Deterministic contracts for error conversion, type guards, NetBox helpers, and WebSocket utility boundaries |
| `test_database_startup.py` | Typed SQLite path/URL resolution, raw-query truncation refusal, inaccessible/default/explicit legacy-auth guard, canonical claim validation, single-worker audited override/marker/reuse refusal, four-process override rejection and schema serialization, fatal migration/post-schema reads, WAL/write rollback, runtime lease, read-only failures, import safety, and lifespan contracts |
| `test_endpoint_crud.py` | Authenticated HTTP CRUD coverage for NetBox and Proxmox endpoint routes |
| `test_ensure_device_overwrite_flags.py` | `_ensure_device` overwrite-flag plumbing for cluster/storage/node-interface/IP tag groups |
| `test_error_handling.py` | Exception hierarchy and HTTP error response shaping |
| `test_fetch_concurrency_kwarg.py` | `PROXBOX_FETCH_MAX_CONCURRENCY` and per-call concurrency overrides |
| `test_generated_proxmox_routes.py` | Runtime registration of generated Proxmox proxy routes |
| `test_health.py` | Health check and root metadata endpoints |
| `test_hardware_discovery_nic_mac.py` | Default-off physical-NIC MAC opt-in, dual-gate resolution, native `dcim.MACAddress` reconciliation, interface targeting, and per-NIC failure isolation |
| `test_individual_sync.py` | Individual per-object sync service and dry-run workflows |
| `test_log_buffer.py` | Ring buffer behavior, level filtering, pagination |
| `test_logger_settings.py` | Logger configuration via env vars |
| `test_main_smoke.py` | Root metadata/version auth behavior and codegen pipeline smoke checks |
| `test_router_smoke.py` | Per-router-prefix HTTP smoke: public routes reachable without auth, every protected prefix returns 401 unauthenticated and exists in the live OpenAPI schema, and safe read endpoints (`/version`, `/cache`, `/cache/metrics`, `/clear-cache`, `/auth/keys`) dispatch end-to-end with a valid API key |
| `test_overwrite_flags_contract.py` | `SyncOverwriteFlags` schema contract and field defaults |
| `test_cloud_image_pipeline.py` | Cloud Image Pipeline catalog/rendering, delimiter-proof encoded writes, typed source recipes, legacy storage, secret-safe ASGI validation, exact isolated SSH argv/host-key pinning, HTTP auth, broad-write + narrow-packer gate ordering, and execution/direct-SDK boundaries |
| `test_packer_preflight.py` | Provider-derived preflight storage/snippet behavior, clean-process import boundary, exact-session/read-only behavior, fail-closed payload/storage-state findings, real session/log canaries, preview rules, OpenAPI contracts, and producer fixture validation |
| `test_packer_execution_binding.py` | Keyed endpoint/recipe bindings and oracle canaries, signed-plan tamper/drift/expiry rejection, retained recovery blockers, expired/concurrent leases, authoritative post-preflight endpoint refresh, cancel/completion CAS, repeated-cancellation journal durability, final artifact verification, minimal session authority, and a producer-owned consumer-shaped fixture that does not claim downstream validation |
| `test_patchable_fields.py` | NetBox PATCH field allowlists and merge semantics |
| `test_plugin_integration.py` | NetBox plugin integration handshake and config |
| `test_role_resolution.py` | VM default-role hierarchy, durable role-snapshot truth table, and verified compensation retries |
| `test_proxmox_codegen_docs.py` | Code generation documentation accuracy |
| `test_proxmox_ha_routes.py` | `/proxmox/cluster/ha/*` aggregation, runtime-state merge, vm/ct fallback in `by-vm`, parallel composition in `summary`, and live router-prefix registration |
| `test_proxmox_sdk_dependency.py` | Verifies `proxbox_api` can import the `proxmox_sdk` mock entrypoint |
| `test_proxmox_to_netbox_contracts.py` | VM mapper behavior and generated schema availability checks |
| `test_pydantic_generator_models.py` | Pydantic model generation from OpenAPI specs |
| `test_qemu_guest_agent_helpers.py` | QEMU guest agent utility functions |
| `test_qemu_guest_agent_sync.py` | QEMU guest agent sync workflows |
| `reconciliation/test_rust_bridge_python.py` | Pydantic bridge serialization and optional Rust import behavior |
| `reconciliation/test_vm_queue_engine_modes.py` | `python`, `compare`, and `rust` reconciliation engine-mode behavior |
| `reconciliation/test_vm_queue_parity.py` | Rust/Python fixture parity for VM operation queues |
| `reconciliation/test_vm_queue_python.py` | Python VM operation-queue contract and edge-case semantics |
| `test_replications_backup_routines_sync.py` | Replication and backup-routine sync workflows |
| `test_schema_contracts.py` | Pydantic schema validation and contract checks |
| `test_session_and_helpers.py` | Session factory creation and dependency wiring |
| `test_settings_client.py` | Settings/plugin-config client (`ProxboxPluginSettings`) accessors |
| `test_snapshots_sync.py` | VM snapshot sync workflow |
| `test_sse_stream_output.py` | SSE event formatting and stream transport |
| `test_storage_sync.py` | Storage discovery and sync workflow |
| `test_streaming_detailed_messages.py` | Detailed-message streaming payload shape |
| `test_structured_logging.py` | `SyncPhaseLogger` operation phase logging |
| `test_stub_routes.py` | HTTP 501 stub endpoints for unimplemented operations |
| `test_sync_active.py` | `GET /sync/active` soft probe + `sync_state` registry lifecycle (issue #71) |
| `test_sync_error_handling.py` | `@with_retry` decorator and domain error wrapping |
| `test_sync_overwrite_flags.py` | Behavior of `SyncOverwriteFlags` propagation through the sync pipeline |
| `test_sync_state_reader.py` | Typed sidecar-first VM identity/name/role reads and legacy fallback contracts |
| `test_sync_state_writer.py` | Typed sidecar writes, including best-effort reflection fields and required/retried post-success role ownership evidence with exact snapshot compensation |
| `test_task_history_sync.py` | Task history sync workflow |
| `test_virtual_disks_sync.py` | Virtual disk sync workflow |
| `test_vm_backup_volids.py` | VM backup volume ID parsing and normalization |
| `test_vm_network.py` | VM network interface mapping and IP address handling |
| `test_netbox_version.py` | `detect_netbox_version` caching, `parse_netbox_version` parsing, `ensure_vm_type` version-gate and pre-resolved `netbox_version` short-circuit |
| `test_vm_sync.py` | Full VM sync workflow including coordinator and dry-run |
| `test_vm_sync_reconciliation_queue.py` | Reconciliation queue draining, role/snapshot rollback, commit-before-response-loss recovery, retry semantics, failure isolation, and empty-queue short-circuit |
| `test_vm_sync_two_phase.py` | Two-phase full-update VM batch (fetch phase vs. process phase ordering), multi-cluster parallel precompute, and cluster precompute failure propagation |
| `test_auth_lockout.py` | Composite credential isolation plus shared source-abuse limits, database-bound and process-pinned identity-key generations/loss/skew/post-start-mutation contracts, atomic key-file publish fault injection, renewable per-token owner leases capped by persisted terminal deadlines, wedged-verifier reclamation and late-result discard, exactly-once duplicate/late-finalizer recovery within the supported horizon, finalizer-driven orphan compaction, credential-cohort coalescing with per-rejection source charging, missing-header and rotating-identity saturation with fair valid admission, bounded active-key recovery scans, unified per-bucket/global admission limits, typed WebSocket auth frames, sync/async/HTTP/WebSocket valid bursts above failure thresholds, durable source budgets/counters, safe expired-row eviction, real ASGI/Uvicorn middleware-stack proxy spoofing and trusted-forwarding partitioning, shell-level nginx bundled/custom-command trust defaults, sync/async and multiprocess atomic races, busy-timeout-before-WAL sync/async contention, full serialized bootstrap, rollback-compatible legacy schema, strict bucket/reservation/metric/key-binding validation, and label-free capacity/row/in-flight/orphan/compaction metrics |
| `test_auth_lockout_cli.py` | Explicit-existing-database enforcement, exact startup-equivalent recovery-schema validation (including destructive-rebind no-mutation cases for malformed PK/type/CHECK definitions), read-only secret-safe inspection/recovery while HTTP is locked, and offline runtime-lease-enforced identity-key rebind that atomically clears buckets/reservations and advances or safely recreates the generation |
| `test_auth_bootstrap.py` | One-shot bootstrap claim: atomic first-key registration, concurrent-claim 409, inactive-history keeps bootstrap closed, legacy backfill idempotency, final-active-key delete/deactivate guards, transactional create/reactivate active-key caps, rotation flow |
| `test_schema_cli.py` | `proxbox-schema` CLI subcommands (`list`, `status`, `generate`) via argparse |
| `test_ensure_tag_duplicate_recovery.py` | `ensure_tag_async` concurrent-creation race recovery: slug/name fallback lookups, re-raise on miss, non-duplicate passthrough |
| `e2e/conftest.py` | E2E fixtures: `proxmox_mock_http_published`, `proxmox_mock_backend`, `client_with_fake_netbox`, `auth_headers` |
| `e2e/test_backups_sync.py` | Backup sync end-to-end against mock backend / HTTP mock |
| `e2e/test_demo_auth.py` | Demo auth happy-path and failure modes |
| `e2e/test_devices_sync.py` | Device sync end-to-end against mock backend / HTTP mock |
| `e2e/test_vm_sync.py` | VM sync end-to-end including overwrite flags and tag preservation |

## Markers

The pytest suite defines two markers in `pyproject.toml`:

- `mock_backend` — tests using the in-process `MockBackend` (fast, no HTTP layer).
- `mock_http` — tests using the HTTP mock container (realistic, validates the HTTP layer).

`unit` and `integration` are directory conventions, not pytest markers.

## Running Tests

```bash
# Full suite
uv run pytest tests

# Single file
uv run pytest tests/test_vm_sync.py

# With coverage (local / GitHub CI shape; the Gitea gate runs statement-only
# coverage with COVERAGE_CORE=sysmon and -n 8 --dist worksteal to fit its
# runner-pool timeout — see .gitea/workflows/ci.yml and
# tests/test_release_workflows.py)
uv run pytest tests/ -n auto \
  --ignore=tests/e2e \
  --ignore=tests/test_generated_proxmox_routes.py \
  --cov=proxbox_api \
  --cov-branch \
  --cov-report=term-missing \
  --cov-report=xml:coverage.xml

# Release-only unit/static contract. GitHub CI additionally prepares the real
# CPython 3.13 musllinux wheelhouse and builds the extracted sdist context with
# Docker build networking disabled.
uv run pytest tests/test_release_workflows.py -q

# E2E tests against in-process MockBackend
uv run pytest tests/e2e -m mock_backend

# E2E tests against HTTP mock container (requires the proxmox-mock service running)
uv run pytest tests/e2e -m mock_http

# VM reconciliation contract and optional Rust parity tests
uv run pytest tests/reconciliation -q
PROXBOX_RECONCILIATION_ENGINE=compare \
  PROXBOX_RECONCILIATION_COMPARE_STRICT=true \
  uv run pytest tests/reconciliation -q
```

The non-E2E core suite enforces a 65.40% branch-inclusive ratchet from a 65.51%
measured baseline (2026-07-17). Generated schema output and `proxbox_api/e2e/`
support code are excluded from this core metric; the latter is exercised by the
separate Docker E2E matrix. Gitea feature pushes and pull requests run the core
gate on the isolated `ci-untrusted-python312` runner after
N-MultiCloud/nmulticloud-context#204 provisions it, and mirrored GitHub CI repeats it
on protected branches. The long-term target is 85%.

## Conventions

- Use `conftest.py` fixtures for app wiring and session mocks — do not create clients inline.
- Name test functions `test_<behavior>_<condition>` (e.g., `test_vm_sync_skips_templates`).
- `proxmox_sdk` is the canonical mock source for Proxmox API responses.
- Keep each test file scoped to one module or workflow; cross-cutting concerns go in `fixtures.py`.
- The global `tests/conftest.py` sets `PROXBOX_RATE_LIMIT=999999` at module-import time so SlowAPI does not trip during the suite.
- The global test environment selects a per-process absolute `PROXBOX_DATABASE_PATH` before importing the app and disables discovery of real host legacy candidates. Production uses a user-data default outside containers and `/data/database.db` inside them; tests must never inspect a host control-plane database. Dedicated startup tests replace the candidate provider only with synthetic temp paths.
- Reconciliation fixtures must stay deterministic. Include `vm_type` in VM
  identity expectations so QEMU and LXC resources with the same VMID do not
  collide.
- Packer preflight fakes must reject every non-GET method. Use realistic
  Proxmox configured content (`images`, `rootdir`, `iso`, `vztmpl`, `backup`,
  `snippets`); never model `import` as configured storage content because it is
  the separate download-url POST request value. Treat malformed collection
  shapes and absent active/enabled storage state as `unsupported`, never as an
  empty or healthy result. Exercise `cluster/nextid?vmid=` as the authoritative
  allocation check, including RBAC-hidden collisions, denial, and malformed
  payloads; resource enumeration is supplemental only. Put credential-bearing
  canaries in subprocess, session, direct-SDK, and cleanup failures and assert
  absence from both serialized responses and the explicitly attached
  non-propagating app logger.
- Route security tests for preflight must include the deployed FastAPI/Starlette
  middleware stack through `test_client` or `auth_test_client`; helper-level
  invocation alone does not prove authentication or exception-middleware
  behavior. Cover auth failure, success, disabled endpoint, malformed upstream
  payloads, and exact-once cleanup.
- Session lifecycle tests must cover post-version failures in cluster status
  and fingerprint discovery, cancellation, cleanup failure, and repeat-close
  behavior while proving the original exception survives and each acquired SDK
  closes exactly once.

## TestClient Fixtures (conftest.py)

Use these fixtures for synchronous HTTP integration tests. They drive the full FastAPI lifespan (startup/shutdown) through the context manager, so generated Proxmox routes and other lifespan-dependent state are available inside each test.

| Fixture | Type | Auth | Use when |
|---------|------|------|----------|
| `test_client` | `fastapi.testclient.TestClient` (sync) | None | Testing auth-exempt routes (`/`, `/health`) or verifying that protected routes reject unauthenticated requests |
| `auth_test_client` | `fastapi.testclient.TestClient` (sync) | `X-Proxbox-API-Key` pre-set | Testing protected routes in synchronous test functions |
| `authenticated_client` | `httpx.AsyncClient` (async) | `X-Proxbox-API-Key` pre-set | Testing protected routes in `async def` test functions, including SSE streaming via `.stream()` |

Both sync fixtures depend on `client_with_fake_netbox` (which sets up the DB override and fake NetBox session). `auth_test_client` additionally depends on `test_api_key`.
