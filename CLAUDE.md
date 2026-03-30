# proxbox-api Project Guide

## Overview

proxbox-api is a FastAPI backend that coordinates data flow between Proxmox clusters and NetBox resources. It exposes REST and websocket endpoints for discovery, synchronization, and status tracking of infrastructure objects such as clusters, nodes, virtual machines, and backups.

## CLAUDE Index

Use this root guide first, then jump to the nearest scoped guide for the area you are changing.

- `nextjs-ui/CLAUDE.md`
- `proxbox_api/CLAUDE.md`
- `proxbox_api/custom_objects/CLAUDE.md`
- `proxbox_api/diode/CLAUDE.md`
- `proxbox_api/enum/CLAUDE.md`
- `proxbox_api/enum/netbox/CLAUDE.md`
- `proxbox_api/enum/netbox/dcim/CLAUDE.md`
- `proxbox_api/enum/netbox/virtualization/CLAUDE.md`
- `proxbox_api/generated/CLAUDE.md`
- `proxbox_api/generated/netbox/CLAUDE.md`
- `proxbox_api/generated/proxmox/CLAUDE.md`
- `proxbox_api/proxmox_codegen/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md`
- `proxbox_api/routes/CLAUDE.md`
- `proxbox_api/routes/dcim/CLAUDE.md`
- `proxbox_api/routes/extras/CLAUDE.md`
- `proxbox_api/routes/netbox/CLAUDE.md`
- `proxbox_api/routes/proxbox/CLAUDE.md`
- `proxbox_api/routes/proxbox/clusters/CLAUDE.md`
- `proxbox_api/routes/proxmox/CLAUDE.md`
- `proxbox_api/routes/virtualization/CLAUDE.md`
- `proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md`
- `proxbox_api/schemas/CLAUDE.md`
- `proxbox_api/schemas/netbox/CLAUDE.md`
- `proxbox_api/schemas/netbox/dcim/CLAUDE.md`
- `proxbox_api/schemas/netbox/extras/CLAUDE.md`
- `proxbox_api/schemas/netbox/virtualization/CLAUDE.md`
- `proxbox_api/schemas/virtualization/CLAUDE.md`
- `proxbox_api/services/CLAUDE.md`
- `proxbox_api/services/sync/CLAUDE.md`
- `proxbox_api/session/CLAUDE.md`
- `proxbox_api/utils/CLAUDE.md`

## Architecture

### Layers

- API layer (`proxbox_api/app/*` factory, `proxbox_api/main.py` entrypoint, `proxbox_api/routes/*`): FastAPI app composition, route registration, endpoint handlers, websocket handlers, and response modeling.
- Session and dependency layer (`proxbox_api/session/*`, `proxbox_api/dependencies.py`): Establishes NetBox and Proxmox client sessions and provides FastAPI dependency aliases.
- Service layer (`proxbox_api/services/sync/*`): Synchronization workflows for creating and updating NetBox objects from Proxmox data.
- Schema and enum layer (`proxbox_api/schemas/*`, `proxbox_api/enum/*`): Pydantic models and enums for validation, payload normalization, and response contracts.
- Persistence layer (`proxbox_api/database.py`): SQLite-backed SQLModel table for NetBox endpoint bootstrap and runtime session creation.
- Utility layer (`proxbox_api/utils/*`, `proxbox_api/logger.py`, `proxbox_api/cache.py`, `proxbox_api/exception.py`): Cross-cutting helpers for logging, exception formatting, in-memory cache, and sync lifecycle tracking.
- Proxmox codegen layer (`proxbox_api/proxmox_codegen/*`): Playwright crawl, `apidoc.js` parsing, OpenAPI conversion, and Pydantic v2 model generation for Proxmox endpoints.
- Proxmox-to-NetBox transform layer (`proxbox_api/proxmox_to_netbox/*`): Pydantic-driven normalization from raw Proxmox payloads to valid NetBox create payloads with live schema contract resolution.

### Runtime Components

- FastAPI app object: Built by `proxbox_api.app.factory.create_app()`; `proxbox_api.main` imports `app` and re-exports symbols used by tests (e.g. `full_update_sync`, `create_virtual_machines`).
- Application lifespan: Registers generated live Proxmox proxy routes (`register_generated_proxmox_routes`); failures are logged and optionally fatal (see **Environment variables**).
- Bootstrap state: `proxbox_api.app.bootstrap` holds `database_session`, `netbox_session`, `netbox_endpoints`, `init_ok`, and `last_init_error` after `init_database_and_netbox()` runs at app creation time.
- NetBox API client: Built from a stored endpoint record in SQLite via `get_netbox_session` (dependency and `bootstrap.netbox_session` for legacy WebSocket paths).
- Proxmox API clients: Built dynamically from DB or NetBox plugin endpoint records via `ProxmoxSession` and `proxmox_sessions` dependency.
- Sync process tracking: Implemented through the `sync_process` decorator and journal entry creation against NetBox plugin objects.

## Entrypoints

- Application entrypoint: `proxbox_api/main.py` (FastAPI app named `app`).
- Typical ASGI command: `uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000`.
- Docker entrypoint: defined in `Dockerfile` with the same module path.
- Test entrypoint: `proxbox_api/test_main.py` using `fastapi.testclient.TestClient`.

## Dependencies

### Core runtime dependencies

Defined in `pyproject.toml`:

- `fastapi[standard]`: API framework and ASGI runtime support.
- `proxmoxer`: Proxmox API client.

- `netbox-sdk`: async facade and request tooling used for NetBox object operations.
- `sqlmodel`: SQLite model and session management.

### Test dependencies

Defined in `pyproject.toml` under `[project.optional-dependencies]` -> `test` (install with `uv sync --extra test --group dev`):

- `pytest`
- `httpx`
- `playwright`
- `pytest-cov`

### Environment variables

- `PROXBOX_NETBOX_TIMEOUT`: NetBox API client timeout in seconds (default: `120`). Controls `netbox-sdk` `Config.timeout` and `aiohttp` request timeouts.
- `PROXBOX_VM_SYNC_MAX_CONCURRENCY`: Maximum concurrent VM creation tasks during sync (default: `4`). Uses an `asyncio.Semaphore` to limit parallel NetBox API load.
- `PROXBOX_CORS_EXTRA_ORIGINS`: Comma-separated extra CORS origins (see `proxbox_api.app.cors.build_cors_origins`).
- `PROXBOX_EXPOSE_INTERNAL_ERRORS`: When set to `1`, `true`, or `yes`, unhandled exceptions return `detail` and `python_exception` derived from the raw exception in the JSON 500 body. When unset (default), the client receives a generic `detail` and `python_exception: null`; the full traceback is logged server-side only (`proxbox_api.app.exceptions`).
- `PROXBOX_STRICT_STARTUP`: When set to `1`, `true`, or `yes`, failure to mount generated Proxmox proxy routes during lifespan raises `ProxboxException` and fails startup instead of logging a warning only (`proxbox_api.app.factory`).

## Runtime Flow

### Startup

1. `create_app()` runs `proxbox_api.app.bootstrap.init_database_and_netbox()`: create tables, open a DB session, build the default NetBox client, set `NetBoxBase.nb`, load `netbox_endpoints` for CORS (with retry on `OperationalError`). Failures are logged; `init_ok` / `last_init_error` record outcome; `netbox_session` and `NetBoxBase.nb` are cleared on bootstrap failure.
2. Construct `FastAPI` with lifespan hook that calls `register_generated_proxmox_routes` (warn or strict per `PROXBOX_STRICT_STARTUP`).
3. Mount static files, CORS middleware (`build_cors_origins`), and global exception handlers.
4. Include root routers from `proxbox_api.app.*` (cache, sync-processes, full-update, websockets) and domain routers (admin, netbox, proxmox, dcim, virtualization, extras).
5. `main.py` imports `app` from the factory path for ASGI servers and test clients.

### Synchronization flow (high level)

1. Endpoint receives request (HTTP or websocket) and resolves dependencies.
2. Proxmox sessions are created from endpoint data fetched through NetBox plugin APIs.
3. Cluster status and resource endpoints gather source inventory from Proxmox.
4. Service and route workflows create or update NetBox objects (clusters, devices, VMs, interfaces, backups).
5. Sync metadata is recorded in NetBox sync-process objects and journal entries.
6. Optional websocket messages stream progress updates to clients.
7. SSE streaming endpoints (`/full-update/stream`, `/devices/create/stream`, `/virtualization/virtual-machines/create/stream`) proxy sync progress via `text/event-stream`. The `WebSocketSSEBridge` utility converts websocket-style progress JSON into SSE frames with per-object granularity (e.g., `Processing device pve01`, `Synced virtual_machine vm101`).

### Error handling

- **Domain errors:** Use `ProxboxException` for predictable API failures. Handler in `proxbox_api.app.exceptions` returns HTTP 400 JSON with `message`, `detail`, and `python_exception`.

- **Unhandled errors:** The same module registers a catch-all `Exception` handler (HTTP 500). By default the response hides internal details from clients; set `PROXBOX_EXPOSE_INTERNAL_ERRORS` or run with `app.debug` true to return `str(exc)` in `detail` / `python_exception`. Full exceptions are always logged with `logger.exception`.

- **Bootstrap:** Database or NetBox client initialization failures are logged at ERROR; operators can inspect `proxbox_api.app.bootstrap.init_ok` and `last_init_error` for health or diagnostics. A missing NetBox session must not leave a stale `NetBoxBase.nb` reference after a failed init.

- **Lifespan / generated routes:** If `register_generated_proxmox_routes` raises `ProxboxException`, startup continues by default with a WARNING log unless `PROXBOX_STRICT_STARTUP` is enabled.

- **Proxmox dependency validation:** Invalid `endpoint_ids` query (non-empty but not a comma-separated list of integers) raises `ProxboxException` instead of silently ignoring the filter (`proxbox_api.session.proxmox_providers`).

- **WebSockets:** `/ws/virtual-machines` returns immediately after a failed `accept` and does not run VM sync. If `bootstrap.netbox_session` is `None`, the handler sends an explanatory text message and closes with code `1011` before calling `create_virtual_machines`. `/ws` returns early if accept fails (no follow-up greeting on a dead socket); operational messages use `logger` instead of `print`.

- **REST reconcile scan:** `rest_reconcile_async` may skip malformed records when scanning for duplicates; skips are logged at DEBUG in `proxbox_api.netbox_rest` to aid troubleshooting.

- **Blocking async bridge:** `proxbox_api.netbox_async_bridge.run_coroutine_blocking` runs `asyncio.run` in a daemon thread when a loop is already running; it does not inherit caller contextvars and has no built-in timeout (see module docstring).

- **Virtual machine read routes:** `GET .../virtual-machines/{id}` returns **404** when not found and **502** on NetBox errors (no empty `{}` body). Stub routes (`/{id}/summary`, interface helpers) return **501** with explicit `detail` (`read_vm.py`).

- **Observability:** Prefer `proxbox_api.logger.logger` over `print` in application code; journal and sync warnings in services/routes should use WARNING level.

## Testing and Verification

### Pre-commit Checklist

Before pushing any changes, run these checks locally using `rtk`:

```bash
# Lint and format check
rtk ruff check .
rtk ruff format --check .

# Bytecode compile check
uv run python -m compileall proxbox_api scripts tests

# Import smoke checks
uv run python -c "import proxbox_api.main"
uv run python -c "from proxbox_api.proxmox_to_netbox.proxmox_schema import load_proxmox_generated_openapi; assert load_proxmox_generated_openapi().get('paths')"

# Run unit tests
rtk pytest tests
```

If any check fails, fix locally until all checks pass before pushing.

### Existing tests

- `proxbox_api/test_main.py` validates the root endpoint response using FastAPI TestClient.

## Coding Conventions

- Python version target: `>=3.10`.
- Keep modules import-safe; avoid side effects beyond required startup wiring.
- Prefer explicit Pydantic models and typed aliases (`Annotated[...]`) for dependencies.
- Raise `ProxboxException` for predictable API error payloads.
- Keep route handlers focused on request orchestration; move reusable logic to services or utilities.
- Maintain ASCII-only content in documentation and source text unless a file already requires otherwise.
- Add concise module-level docstrings to all Python modules.
- **ALL normalization and parsing MUST be done inside Pydantic schemas.** See `proxbox_api/proxmox_to_netbox/CLAUDE.md` for details.

## Directory Map

- `proxbox_api/`: app bootstrap, shared dependencies, database, cache, logging, exceptions.
- `proxbox_api/routes/`: API route groups.
- `proxbox_api/services/`: sync-oriented business logic.
- `proxbox_api/session/`: NetBox and Proxmox session factories.
- `proxbox_api/schemas/`: Pydantic schemas for payloads and contracts.
- `proxbox_api/enum/`: enum values used by schemas and routes.
- `proxbox_api/utils/`: decorators and helper utilities.
- `proxbox_api/custom_objects/`: custom NetBox object wrappers.
- `proxbox_api/proxmox_codegen/`: Proxmox API viewer extraction and schema generation pipeline.
- `proxbox_api/generated/proxmox/`: generated OpenAPI and Pydantic artifacts.
- `proxbox_api/proxmox_to_netbox/`: Proxmox input to NetBox output schema transformations.
- `proxbox_api/proxmox_to_netbox/schemas/`: Schema-driven parsing modules (disk parsing, etc.).
- `proxbox_api/generated/netbox/`: cached NetBox OpenAPI contract artifacts.

## Safe Extension Pattern

1. Define or update schemas and enums first.
2. Add business logic in service modules.
3. Wire dependencies and expose endpoint handlers in routes.
4. Register routers in `proxbox_api.app.factory.create_app()` (or the appropriate `app.include_router` in a dedicated router module).
5. Add or update tests under `proxbox_api/`.
6. Run compile and test checks before submitting changes.
