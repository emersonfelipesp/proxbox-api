# proxbox-api Project Guide

## Overview

proxbox-api is a FastAPI backend that coordinates data flow between Proxmox clusters and NetBox resources. It exposes REST and websocket endpoints for discovery, synchronization, and status tracking of infrastructure objects such as clusters, nodes, virtual machines, and backups.

## Architecture

### Layers

- API layer (`proxbox_api/main.py` and `proxbox_api/routes/*`): FastAPI app setup, route registration, endpoint handlers, websocket handlers, and response modeling.
- Session and dependency layer (`proxbox_api/session/*`, `proxbox_api/dependencies.py`): Establishes NetBox and Proxmox client sessions and provides FastAPI dependency aliases.
- Service layer (`proxbox_api/services/sync/*`): Synchronization workflows for creating and updating NetBox objects from Proxmox data.
- Schema and enum layer (`proxbox_api/schemas/*`, `proxbox_api/enum/*`): Pydantic models and enums for validation, payload normalization, and response contracts.
- Persistence layer (`proxbox_api/database.py`): SQLite-backed SQLModel table for NetBox endpoint bootstrap and runtime session creation.
- Utility layer (`proxbox_api/utils/*`, `proxbox_api/logger.py`, `proxbox_api/cache.py`, `proxbox_api/exception.py`): Cross-cutting helpers for logging, exception formatting, in-memory cache, and sync lifecycle tracking.
- Proxmox codegen layer (`proxbox_api/proxmox_codegen/*`): Playwright crawl, `apidoc.js` parsing, OpenAPI conversion, and Pydantic v2 model generation for Proxmox endpoints.

### Runtime Components

- FastAPI app object: Created in `proxbox_api/main.py` with CORS middleware and router inclusion.
- NetBox API client: Built from a stored endpoint record in SQLite via `get_netbox_session`.
- Proxmox API clients: Built dynamically from NetBox plugin endpoint objects via `ProxmoxSession`.
- Sync process tracking: Implemented through the `sync_process` decorator and journal entry creation against NetBox plugin objects.

## Entrypoints

- Application entrypoint: `proxbox_api/main.py` (FastAPI app named `app`).
- Typical ASGI command: `uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8800`.
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

Defined in `requirements-test.txt`:

- `pytest`
- `httpx`

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

### Error handling

- Domain-specific errors use `ProxboxException`.
- FastAPI exception handler in `main.py` returns structured JSON for `ProxboxException`.
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

## Safe Extension Pattern

1. Define or update schemas and enums first.
2. Add business logic in service modules.
3. Wire dependencies and expose endpoint handlers in routes.
4. Register routers in `main.py`.
5. Add or update tests under `proxbox_api/`.
6. Run compile and test checks before submitting changes.
