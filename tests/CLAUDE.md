# tests/ Directory Guide

## Purpose

Unit, integration, and end-to-end tests for the `proxbox_api` backend package. All tests here run against the `proxbox_api` package with dependency-injected mocks for NetBox and Proxmox sessions. True E2E browser tests live in `tests/e2e/`.

## Test File Index

| File | What it tests |
|------|---------------|
| `conftest.py` | Global fixtures: test DB engine, FastAPI test app, dependency overrides, fake NetBox session |
| `fixtures.py` | Shared reusable fixtures imported by multiple test modules |
| `test_api_routes.py` | API route integration tests (request/response contracts) |
| `test_admin_logs.py` | In-memory log buffer routes (`/admin/logs`) |
| `test_backups_vm_sync.py` | VM backup discovery and sync workflow |
| `test_error_handling.py` | Exception hierarchy and HTTP error response shaping |
| `test_generated_proxmox_routes.py` | Runtime registration of generated Proxmox proxy routes |
| `test_health.py` | Health check and root metadata endpoints |
| `test_individual_sync.py` | Individual per-object sync service and dry-run workflows |
| `test_log_buffer.py` | Ring buffer behavior, level filtering, pagination |
| `test_plugin_integration.py` | NetBox plugin integration handshake and config |
| `test_proxmox_codegen_docs.py` | Code generation documentation accuracy |
| `test_proxmox_sdk_dependency.py` | Verifies `proxbox_api` can import the `proxmox_sdk` mock entrypoint |
| `test_pydantic_generator_models.py` | Pydantic model generation from OpenAPI specs |
| `test_qemu_guest_agent_helpers.py` | QEMU guest agent utility functions |
| `test_qemu_guest_agent_sync.py` | QEMU guest agent sync workflows |
| `test_schema_contracts.py` | Pydantic schema validation and contract checks |
| `test_session_and_helpers.py` | Session factory creation and dependency wiring |
| `test_snapshots_sync.py` | VM snapshot sync workflow |
| `test_sse_stream_output.py` | SSE event formatting and stream transport |
| `test_storage_sync.py` | Storage discovery and sync workflow |
| `test_structured_logging.py` | `SyncPhaseLogger` operation phase logging |
| `test_stub_routes.py` | HTTP 501 stub endpoints for unimplemented operations |
| `test_sync_error_handling.py` | `@with_retry` decorator and domain error wrapping |
| `test_task_history_sync.py` | Task history sync workflow |
| `test_virtual_disks_sync.py` | Virtual disk sync workflow |
| `test_vm_backup_volids.py` | VM backup volume ID parsing and normalization |
| `test_vm_sync.py` | Full VM sync workflow including coordinator and dry-run |
| `e2e/` | Playwright browser-backed end-to-end tests |

## Running Tests

```bash
# Full suite
uv run pytest tests

# Single file
uv run pytest tests/test_vm_sync.py

# With coverage
uv run pytest --cov=proxbox_api --cov-report=xml tests

# E2E tests (requires Playwright browsers)
uv run pytest tests/e2e
```

## Conventions

- Use `conftest.py` fixtures for app wiring and session mocks — do not create clients inline.
- Name test functions `test_<behavior>_<condition>` (e.g., `test_vm_sync_skips_templates`).
- `proxmox_sdk` is the canonical mock source for Proxmox API responses.
- Keep each test file scoped to one module or workflow; cross-cutting concerns go in `fixtures.py`.
- Mark slow tests with `@pytest.mark.slow` and skip in CI fast-path if needed.
