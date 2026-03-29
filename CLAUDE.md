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

- API layer (`proxbox_api/main.py` and `proxbox_api/routes/*`): FastAPI app setup, route registration, endpoint handlers, websocket handlers, and response modeling.
- Session and dependency layer (`proxbox_api/session/*`, `proxbox_api/dependencies.py`): Establishes NetBox and Proxmox client sessions and provides FastAPI dependency aliases.
- Service layer (`proxbox_api/services/sync/*`): Synchronization workflows for creating and updating NetBox objects from Proxmox data.
- Schema and enum layer (`proxbox_api/schemas/*`, `proxbox_api/enum/*`): Pydantic models and enums for validation, payload normalization, and response contracts.
- Persistence layer (`proxbox_api/database.py`): SQLite-backed SQLModel table for NetBox endpoint bootstrap and runtime session creation.
- Utility layer (`proxbox_api/utils/*`, `proxbox_api/logger.py`, `proxbox_api/cache.py`, `proxbox_api/exception.py`): Cross-cutting helpers for logging, exception formatting, in-memory cache, and sync lifecycle tracking.
- Proxmox codegen layer (`proxbox_api/proxmox_codegen/*`): Playwright crawl, `apidoc.js` parsing, OpenAPI conversion, and Pydantic v2 model generation for Proxmox endpoints.
- Proxmox-to-NetBox transform layer (`proxbox_api/proxmox_to_netbox/*`): Pydantic-driven normalization from raw Proxmox payloads to valid NetBox create payloads with live schema contract resolution.

### Runtime Components

- FastAPI app object: Created in `proxbox_api/main.py` with CORS middleware and router inclusion.
- NetBox API client: Built from a stored endpoint record in SQLite via `get_netbox_session`.
- Proxmox API clients: Built dynamically from NetBox plugin endpoint objects via `ProxmoxSession`.
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
- `pynetbox`: NetBox API client.
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

## Runtime Flow

### Startup

1. Import and initialize FastAPI app in `main.py`.
2. Open local SQLite session (`database.db`) and retrieve NetBox endpoint configuration.
3. Build a NetBox client and attach it to compatibility wrappers for model-style operations.
4. Resolve CORS origins from database-backed endpoint records.
5. Register routers for Proxmox, NetBox, DCIM, virtualization, and extras APIs.

### Synchronization flow (high level)

1. Endpoint receives request (HTTP or websocket) and resolves dependencies.
2. Proxmox sessions are created from endpoint data fetched through NetBox plugin APIs.
3. Cluster status and resource endpoints gather source inventory from Proxmox.
4. Service and route workflows create or update NetBox objects (clusters, devices, VMs, interfaces, backups).
5. Sync metadata is recorded in NetBox sync-process objects and journal entries.
6. Optional websocket messages stream progress updates to clients.
7. SSE streaming endpoints (`/full-update/stream`, `/devices/create/stream`, `/virtualization/virtual-machines/create/stream`) proxy sync progress via `text/event-stream`. The `WebSocketSSEBridge` utility converts websocket-style progress JSON into SSE frames with per-object granularity (e.g., `Processing device pve01`, `Synced virtual_machine vm101`).

### Error handling

- Domain-specific errors use `ProxboxException`.
- FastAPI exception handler in `main.py` returns structured JSON for `ProxboxException`.
- A global `@app.exception_handler(Exception)` returns structured JSON for unhandled errors (status 500), including the exception message and traceback string.
- Lower-level exceptions should be wrapped with context before propagation.

## Testing and Verification

### Local checks

- Bytecode compile check: `python -m compileall proxbox_api`
- Unit tests (if dependencies are installed): `pytest`

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
4. Register routers in `main.py`.
5. Add or update tests under `proxbox_api/`.
6. Run compile and test checks before submitting changes.
