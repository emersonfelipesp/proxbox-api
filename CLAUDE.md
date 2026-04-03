# proxbox-api Project Guide

## Overview

`proxbox-api` is a FastAPI backend that connects Proxmox inventory and lifecycle data to NetBox objects. It serves REST, SSE, and WebSocket endpoints for discovery, synchronization, endpoint management, and generated Proxmox proxy routes. The same repository also includes a standalone `nextjs-ui/` frontend for endpoint administration.

## Use This Index First

Open the nearest scoped guide for the code you are changing.

- `nextjs-ui/AGENTS.md`
- `nextjs-ui/CLAUDE.md`
- `proxbox_api/CLAUDE.md`
- `proxbox_api/custom_objects/CLAUDE.md`
- `proxbox_api/diode/CLAUDE.md`
- `proxbox_api/e2e/CLAUDE.md`
- `proxbox_api/enum/CLAUDE.md`
- `proxbox_api/enum/netbox/CLAUDE.md`
- `proxbox_api/enum/netbox/dcim/CLAUDE.md`
- `proxbox_api/enum/netbox/virtualization/CLAUDE.md`
- `proxbox_api/generated/CLAUDE.md`
- `proxbox_api/generated/netbox/CLAUDE.md`
- `proxbox_api/generated/proxmox/CLAUDE.md`
- `proxbox_api/proxmox_codegen/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md`
- `proxbox_api/routes/CLAUDE.md`
- `proxbox_api/routes/admin/CLAUDE.md`
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
- `proxbox_api/services/sync/individual/CLAUDE.md`
- `proxbox_api/session/CLAUDE.md`
- `proxbox_api/utils/CLAUDE.md`

## Repo Structure

- `proxbox_api/`: FastAPI package, session factories, schemas, routes, sync services, code generation, and shared utilities.
- `nextjs-ui/`: Next.js frontend used to manage one NetBox endpoint and multiple Proxmox endpoints.
- `tests/`: Unit, integration, and end-to-end tests for the backend package.
- `docs/`: MkDocs documentation, including English and Brazilian Portuguese content.
- `scripts/`: Utility scripts, including schema refresh helpers.
- `Dockerfile` and `docker/`: runtime and reverse-proxy images for local and published deployments.

## Architecture

### Core layers

- API and app composition (`proxbox_api/app/*`, `proxbox_api/main.py`, `proxbox_api/routes/*`): create the FastAPI app, register routers, mount middleware, expose WebSocket and SSE streams, and keep request handlers thin.
- Session and dependency layer (`proxbox_api/session/*`, `proxbox_api/dependencies.py`): create NetBox and Proxmox client sessions from database or plugin configuration.
- Service layer (`proxbox_api/services/*`): implement synchronization workflows, object reconciliation, and reusable helper logic.
- Schema and enum layer (`proxbox_api/schemas/*`, `proxbox_api/enum/*`): validate payloads, normalize data, and define contract-safe choice values.
- Transform and codegen layer (`proxbox_api/proxmox_to_netbox/*`, `proxbox_api/proxmox_codegen/*`, `proxbox_api/generated/*`): turn Proxmox data into NetBox payloads and generate contract artifacts.
- Support layer (`proxbox_api/utils/*`, `proxbox_api/logger.py`, `proxbox_api/cache.py`, `proxbox_api/exception.py`, `proxbox_api/netbox_rest.py`, `proxbox_api/netbox_async_bridge.py`, `proxbox_api/openapi_custom.py`): logging, streaming, caching, compatibility, and exception helpers.
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

- Runtime: `fastapi[standard]`, `proxmoxer`, `netbox-sdk`, `sqlmodel`
- Tests: `pytest`, `httpx`, `playwright`, `pytest-cov`, `pytest-asyncio`, `pytest-xdist`
- Docs: `mkdocs`, `mkdocs-material`, `mkdocs-static-i18n`

## Environment Variables

- `PROXBOX_NETBOX_TIMEOUT`: NetBox client timeout in seconds.
- `PROXBOX_VM_SYNC_MAX_CONCURRENCY`: limits concurrent VM sync work.
- `PROXBOX_FETCH_MAX_CONCURRENCY`: limits concurrent storage, backup, and snapshot fetches.
- `PROXBOX_CORS_EXTRA_ORIGINS`: extra CORS origins.
- `PROXBOX_EXPOSE_INTERNAL_ERRORS`: returns raw exception details in 500 responses when enabled.
- `PROXBOX_STRICT_STARTUP`: turns generated-route startup failures into fatal startup errors.
- `PROXBOX_SKIP_NETBOX_BOOTSTRAP`: skips default NetBox bootstrap at startup.

## Validation

Run these checks before pushing changes:

```bash
rtk ruff check .
rtk ruff format --check .
uv run python -m compileall proxbox_api tests
uv run python -c "import proxbox_api.main"
uv run python -c "from proxbox_api.proxmox_to_netbox.proxmox_schema import load_proxmox_generated_openapi; assert load_proxmox_generated_openapi().get('paths')"
rtk pytest tests
```

If you touch `nextjs-ui/`, also run:

```bash
cd nextjs-ui
npm run lint
npm run build
```

## Extension Rules

1. Update schemas and enums before route handlers.
2. Put reusable workflow logic in services, not routes.
3. Keep route modules focused on request orchestration and response shaping.
4. Add or update tests for new behavior.
5. Regenerate generated artifacts instead of editing them by hand.
