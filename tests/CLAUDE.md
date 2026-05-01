# tests/ Directory Guide

## Purpose

Unit, integration, and end-to-end tests for the `proxbox_api` backend package. All tests run against the `proxbox_api` package with dependency-injected mocks for NetBox and Proxmox sessions. The `tests/e2e/` subdirectory holds API-level end-to-end tests that wire the full FastAPI app to a `proxmox-sdk` mock (HTTP container or in-process backend) — they use `httpx.AsyncClient`, not Playwright.

## Test File Index

| File | What it tests |
|------|---------------|
| `conftest.py` | Global fixtures: test DB engine, FastAPI test app, dependency overrides, fake NetBox session, auth headers |
| `fixtures.py` | Shared reusable fixtures imported by multiple test modules |
| `test_admin_logs.py` | In-memory log buffer routes (`/admin/logs`) |
| `test_api_routes.py` | API route integration tests (request/response contracts) |
| `test_backups_vm_sync.py` | VM backup discovery and sync workflow |
| `test_bridge_interfaces.py` | VM bridge interface mapping and reconciliation |
| `test_bulk_sync_error_accounting.py` | Per-batch error tallies for bulk VM sync paths |
| `test_credentials.py` | Credential encryption/decryption round-trip and Fernet key resolution |
| `test_endpoint_crud.py` | Authenticated HTTP CRUD coverage for NetBox and Proxmox endpoint routes |
| `test_ensure_device_overwrite_flags.py` | `_ensure_device` overwrite-flag plumbing for cluster/storage/node-interface/IP tag groups |
| `test_error_handling.py` | Exception hierarchy and HTTP error response shaping |
| `test_fetch_concurrency_kwarg.py` | `PROXBOX_FETCH_MAX_CONCURRENCY` and per-call concurrency overrides |
| `test_generated_proxmox_routes.py` | Runtime registration of generated Proxmox proxy routes |
| `test_health.py` | Health check and root metadata endpoints |
| `test_individual_sync.py` | Individual per-object sync service and dry-run workflows |
| `test_log_buffer.py` | Ring buffer behavior, level filtering, pagination |
| `test_logger_settings.py` | Logger configuration via env vars |
| `test_main_smoke.py` | Root metadata/version auth behavior and codegen pipeline smoke checks |
| `test_overwrite_flags_contract.py` | `SyncOverwriteFlags` schema contract and field defaults |
| `test_patchable_fields.py` | NetBox PATCH field allowlists and merge semantics |
| `test_plugin_integration.py` | NetBox plugin integration handshake and config |
| `test_proxmox_codegen_docs.py` | Code generation documentation accuracy |
| `test_proxmox_sdk_dependency.py` | Verifies `proxbox_api` can import the `proxmox_sdk` mock entrypoint |
| `test_proxmox_to_netbox_contracts.py` | VM mapper behavior and generated schema availability checks |
| `test_pydantic_generator_models.py` | Pydantic model generation from OpenAPI specs |
| `test_qemu_guest_agent_helpers.py` | QEMU guest agent utility functions |
| `test_qemu_guest_agent_sync.py` | QEMU guest agent sync workflows |
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
| `test_sync_error_handling.py` | `@with_retry` decorator and domain error wrapping |
| `test_sync_overwrite_flags.py` | Behavior of `SyncOverwriteFlags` propagation through the sync pipeline |
| `test_task_history_sync.py` | Task history sync workflow |
| `test_virtual_disks_sync.py` | Virtual disk sync workflow |
| `test_vm_backup_volids.py` | VM backup volume ID parsing and normalization |
| `test_vm_network.py` | VM network interface mapping and IP address handling |
| `test_vm_sync.py` | Full VM sync workflow including coordinator and dry-run |
| `test_vm_sync_reconciliation_queue.py` | Reconciliation queue draining and retry semantics |
| `test_auth_lockout.py` | bcrypt API-key check, failed-attempt counting, lockout duration, and async path |
| `test_schema_cli.py` | `proxbox-schema` CLI subcommands (`list`, `status`, `generate`) via argparse |
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

# With coverage
uv run pytest --cov=proxbox_api --cov-report=xml tests

# E2E tests against in-process MockBackend
uv run pytest tests/e2e -m mock_backend

# E2E tests against HTTP mock container (requires the proxmox-mock service running)
uv run pytest tests/e2e -m mock_http
```

## Conventions

- Use `conftest.py` fixtures for app wiring and session mocks — do not create clients inline.
- Name test functions `test_<behavior>_<condition>` (e.g., `test_vm_sync_skips_templates`).
- `proxmox_sdk` is the canonical mock source for Proxmox API responses.
- Keep each test file scoped to one module or workflow; cross-cutting concerns go in `fixtures.py`.
- The global `tests/conftest.py` sets `PROXBOX_RATE_LIMIT=999999` at module-import time so SlowAPI does not trip during the suite.
