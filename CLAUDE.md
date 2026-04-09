# proxbox-api Project Guide

## Overview

`proxbox-api` is a FastAPI backend that connects Proxmox inventory and lifecycle data to NetBox objects. It serves REST, SSE, and WebSocket endpoints for discovery, synchronization, endpoint management, and generated Proxmox proxy routes. The same repository also includes a standalone `nextjs-ui/` frontend for endpoint administration.

## Use This Index First

Open the nearest scoped guide for the code you are changing.

### Top-level packages

- `proxbox_api/CLAUDE.md` — Core FastAPI package overview
- `proxmox-openapi/CLAUDE.md` — Proxmox OpenAPI package (mock + real API modes)
- `nextjs-ui/CLAUDE.md` — Next.js frontend for endpoint management
- `nextjs-ui/AGENTS.md` — Frontend agent quick-reference

### Infrastructure and tooling

- `.github/CLAUDE.md` — CI/CD workflow descriptions
- `docker/CLAUDE.md` — Container runtime and proxy configuration
- `docs/CLAUDE.md` — MkDocs documentation structure
- `tests/CLAUDE.md` — Backend test suite layout and conventions
- `scripts/CLAUDE.md` — Utility and maintenance scripts
- `tasks/CLAUDE.md` — Development task tracking
- `automation/CLAUDE.md` — Automation entry points
- `proxmox-mock/CLAUDE.md` — Mock Proxmox service used in tests

### proxbox_api subpackages

- `proxbox_api/app/CLAUDE.md` — Application factory and lifecycle
- `proxbox_api/routes/CLAUDE.md` — Route package index
- `proxbox_api/routes/admin/CLAUDE.md` — Admin dashboard routes
- `proxbox_api/routes/dcim/CLAUDE.md` — DCIM device routes
- `proxbox_api/routes/extras/CLAUDE.md` — Extras (tags, custom fields) routes
- `proxbox_api/routes/netbox/CLAUDE.md` — NetBox endpoint CRUD routes
- `proxbox_api/routes/proxbox/CLAUDE.md` — Proxbox plugin routes
- `proxbox_api/routes/proxbox/clusters/CLAUDE.md` — Cluster route namespace
- `proxbox_api/routes/proxmox/CLAUDE.md` — Proxmox proxy and codegen routes
- `proxbox_api/routes/sync/CLAUDE.md` — Internal sync helper routes
- `proxbox_api/routes/virtualization/CLAUDE.md` — Virtualization routes
- `proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md` — VM sync routes
- `proxbox_api/services/CLAUDE.md` — Service layer index
- `proxbox_api/services/sync/CLAUDE.md` — Sync workflow services
- `proxbox_api/services/sync/individual/CLAUDE.md` — Individual object sync services
- `proxbox_api/session/CLAUDE.md` — Session and client factories
- `proxbox_api/schemas/CLAUDE.md` — Pydantic schema index
- `proxbox_api/schemas/netbox/CLAUDE.md` — NetBox domain schemas
- `proxbox_api/schemas/netbox/dcim/CLAUDE.md` — DCIM schemas
- `proxbox_api/schemas/netbox/extras/CLAUDE.md` — Extras schemas
- `proxbox_api/schemas/netbox/virtualization/CLAUDE.md` — Virtualization schemas
- `proxbox_api/schemas/virtualization/CLAUDE.md` — VM-level schemas
- `proxbox_api/enum/CLAUDE.md` — Enum/choice values index
- `proxbox_api/enum/netbox/CLAUDE.md` — NetBox enums
- `proxbox_api/enum/netbox/dcim/CLAUDE.md` — DCIM enums
- `proxbox_api/enum/netbox/virtualization/CLAUDE.md` — Virtualization enums
- `proxbox_api/proxmox_codegen/CLAUDE.md` — Proxmox API crawler and generator
- `proxbox_api/proxmox_to_netbox/CLAUDE.md` — Proxmox-to-NetBox transformation
- `proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md` — Object mappers
- `proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md` — Transformation schemas
- `proxbox_api/generated/CLAUDE.md` — Generated artifacts (do not edit)
- `proxbox_api/generated/netbox/CLAUDE.md` — NetBox model snapshots
- `proxbox_api/generated/proxmox/CLAUDE.md` — Proxmox model snapshots
- `proxbox_api/types/CLAUDE.md` — Type aliases and protocols
- `proxbox_api/utils/CLAUDE.md` — Shared utilities
- `proxbox_api/custom_objects/CLAUDE.md` — Custom NetBox object wrappers
- `proxbox_api/diode/CLAUDE.md` — Diode sandbox integration
- `proxbox_api/e2e/CLAUDE.md` — E2E browser test helpers

## Repo Structure

- `proxbox_api/`: FastAPI package, session factories, schemas, routes, sync services, code generation, and shared utilities.
- `proxmox-openapi/`: Schema-driven Proxmox API package used for both mock endpoints and real API access.
- `nextjs-ui/`: Next.js frontend used to manage one NetBox endpoint and multiple Proxmox endpoints.
- `tests/`: Unit, integration, and end-to-end tests for the backend package.
- `docs/`: MkDocs documentation, including English and Brazilian Portuguese content.
- `scripts/`: Utility scripts, including schema refresh helpers.
- `automation/`: Placeholder for future automation workflows.
- `tasks/`: Development task tracking.
- `Dockerfile` and `docker/`: runtime and reverse-proxy images for local and published deployments.
- `.github/workflows/`: CI/CD pipelines for test, lint, publish, and docs.

## Architecture

### Core layers

- API and app composition (`proxbox_api/app/*`, `proxbox_api/main.py`, `proxbox_api/routes/*`): create the FastAPI app, register routers, mount middleware, expose WebSocket and SSE streams, and keep request handlers thin.
- Session and dependency layer (`proxbox_api/session/*`, `proxbox_api/dependencies.py`): create NetBox and Proxmox client sessions from database or plugin configuration.
- Service layer (`proxbox_api/services/*`): implement synchronization workflows, object reconciliation, and reusable helper logic.
- Schema and enum layer (`proxbox_api/schemas/*`, `proxbox_api/enum/*`): validate payloads, normalize data, and define contract-safe choice values.
- Transform and codegen layer (`proxbox_api/proxmox_to_netbox/*`, `proxbox_api/proxmox_codegen/*`, `proxbox_api/generated/*`): turn Proxmox data into NetBox payloads and generate contract artifacts.
- Support layer (`proxbox_api/utils/*`, `proxbox_api/logger.py`, `proxbox_api/cache.py`, `proxbox_api/exception.py`, `proxbox_api/netbox_rest.py`, `proxbox_api/openapi_custom.py`): logging, streaming, caching, and exception helpers.
- Demo and e2e layer (`proxbox_api/e2e/*`): Playwright authentication helpers and shared fixtures for browser-backed tests.

### Runtime flow

1. `proxbox_api.app.factory.create_app()` initializes database state, builds the default NetBox session, and records bootstrap status.
2. The app registers generated Proxmox proxy routes during lifespan startup and wires shared middleware, routers, and exception handlers.
3. Requests resolve NetBox and Proxmox sessions through dependency providers.
4. Route handlers delegate heavy work to service modules and schemas.
5. Sync runs emit journal entries, structured logs, and optional WebSocket or SSE progress messages.

### Error and data rules

- Use `ProxboxException` for expected API failures.
- Keep parsing and normalization inside Pydantic schemas, especially in `proxbox_api/proxmox_to_netbox/`.
- Keep generated artifacts under `proxbox_api/generated/` out of manual editing unless you are debugging generation itself.
- Preserve parity between WebSocket progress payloads and SSE payloads.
- Prefer `proxbox_api.logger.logger` over `print`.

## Entry Points

- ASGI app: `proxbox_api.main:app`
- Typical server command: `uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000`
- Docker entrypoint: the `Dockerfile` uses the same app module path.
- Smoke tests: `proxbox_api/test_main.py` and the repository test suite under `tests/`

## Dependencies

- Runtime: `fastapi[standard]`, `proxmox-openapi`, `netbox-sdk`, `sqlmodel`
- Tests: `pytest`, `httpx`, `playwright`, `pytest-cov`, `pytest-asyncio`, `pytest-xdist`
- Docs: `mkdocs`, `mkdocs-material`, `mkdocs-static-i18n`

## Environment Variables

- `PROXBOX_NETBOX_TIMEOUT`: NetBox client timeout in seconds (default: 120).
- `PROXBOX_NETBOX_MAX_CONCURRENT`: max concurrent NetBox API requests (default: 1, keep low to avoid PostgreSQL pool exhaustion).
- `PROXBOX_NETBOX_MAX_RETRIES`: retry attempts for transient failures (default: 5).
- `PROXBOX_NETBOX_RETRY_DELAY`: base retry delay in seconds (default: 2.0).
- `PROXBOX_VM_SYNC_MAX_CONCURRENCY`: limits concurrent VM sync work.
- `PROXBOX_FETCH_MAX_CONCURRENCY`: limits concurrent storage, backup, and snapshot fetches.
- `PROXBOX_CORS_EXTRA_ORIGINS`: extra CORS origins.
- `PROXBOX_EXPOSE_INTERNAL_ERRORS`: returns raw exception details in 500 responses when enabled.
- `PROXBOX_STRICT_STARTUP`: turns generated-route startup failures into fatal startup errors.
- `PROXBOX_SKIP_NETBOX_BOOTSTRAP`: skips default NetBox bootstrap at startup.

### Cache Configuration

- `PROXBOX_NETBOX_GET_CACHE_TTL`: NetBox GET response cache TTL in seconds (default: 60.0, set to 0 to disable)
- `PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES`: maximum cached GET responses by entry count (default: 4096)
- `PROXBOX_NETBOX_GET_CACHE_MAX_BYTES`: maximum cache size in bytes (default: 52428800 = 50MB)
- `PROXBOX_DEBUG_CACHE`: enable debug-level cache logging (default: 0)

## Validation

Run these checks before pushing changes:

```bash
rtk ruff check .
rtk ruff format --check .
uv run python -m compileall proxbox_api tests
uv run python -c "import proxbox_api.main"
uv run python -c "from proxbox_api.proxmox_to_netbox.proxmox_schema import load_proxmox_generated_openapi; assert load_proxmox_generated_openapi().get('paths')"
rtk mypy proxbox_api  # Check type annotations (non-strict mode)
rtk pytest tests
```

If you touch `nextjs-ui/`, also run:

```bash
cd nextjs-ui
npm run lint
npm run build
```

## Type System

The project uses Python's type hints with optional `mypy` checking. Type system conventions:

### Domain Types (Type Aliases)

Use `TypeAlias` for semantic clarity on primitive values:

```python
from proxbox_api.types import RecordID, VMID, ClusterName

def process_device(device_id: RecordID, cluster: ClusterName) -> None:
    """Type-safe device processing with semantic naming."""
```

### Protocols for Duck-Typing

Use `@runtime_checkable` Protocols when working with multiple object types:

```python
from proxbox_api.types import NetBoxRecord, SyncResult

def update_record(record: NetBoxRecord) -> SyncResult:
    """Works with any NetBox object having record interface."""
```

### TypedDicts for Data Structures

Use `TypedDict` when dictionary structure matters:

```python
from proxbox_api.types import VMPayloadDict, DevicePayloadDict

def build_vm_payload(...) -> VMPayloadDict:
    """Type-safe NetBox VM payload with documented fields."""
    return {
        "name": "vm-name",
        "cluster": cluster_id,
        "vcpus": 4,
    }
```

See `proxbox_api/types/CLAUDE.md` for complete typing guidelines.

## Extension Rules

1. Update schemas and enums before route handlers.
2. Put reusable workflow logic in services, not routes.
3. Keep route modules focused on request orchestration and response shaping.
4. Add or update tests for new behavior.
5. Regenerate generated artifacts instead of editing them by hand.
