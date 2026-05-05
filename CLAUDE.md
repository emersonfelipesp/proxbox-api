# proxbox-api Project Guide

## Overview

`proxbox-api` is a FastAPI backend that connects Proxmox inventory and lifecycle data to NetBox objects. It serves REST, SSE, and WebSocket endpoints for discovery, synchronization, endpoint management, and generated Proxmox proxy routes. The same repository also includes a standalone `nextjs-ui/` frontend for endpoint administration.

## Use This Index First

Open the nearest scoped guide for the code you are changing.

### Top-level packages

- `proxbox_api/CLAUDE.md` — Core FastAPI package overview
- `proxmox-sdk/CLAUDE.md` — Proxmox OpenAPI package (mock + real API modes)
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
- `proxmox-sdk/`: Schema-driven Proxmox API package used for both mock endpoints and real API access.
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
- Authentication layer (`proxbox_api/auth.py`, `proxbox_api/routes/auth.py`): bcrypt-hashed API key storage, `X-Proxbox-API-Key` header enforcement via `APIKeyAuthMiddleware`, brute-force lockout, and bootstrap flow for first-time key registration.
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
- CLI: `proxbox-proxmox-codegen` (`proxbox_api.proxmox_codegen.cli:main`) — Proxmox crawler/generator pipeline.
- CLI: `proxbox-schema` (`proxbox_api.schema_cli:main`) — list, status, and generate NetBox-versioned schema artifacts.
- Smoke tests live under `tests/` (for example `tests/test_main_smoke.py` and `tests/test_endpoint_crud.py`)

## Dependencies

- Runtime: `fastapi[standard]`, `proxmox-sdk`, `netbox-sdk`, `sqlmodel`, `aiosqlite`, `cryptography`, `bcrypt`
- Tests: `pytest`, `httpx`, `playwright`, `pytest-cov`, `pytest-asyncio`, `pytest-xdist`
- Docs: `mkdocs`, `mkdocs-material`, `mkdocs-static-i18n`

## Environment Variables

Most runtime tunables now resolve in order **env var > `ProxboxPluginSettings` (NetBox plugin settings page) > built-in default**, via `proxbox_api/runtime_settings.py`. Setting an env var still works as an override; leaving it unset means the plugin settings page is the authoritative source. The settings cache TTL is 5 minutes, so plugin-side changes take effect without a restart.

### Adding a new tunable

**Configuration policy — prefer DB-backed plugin settings.**
When adding a new runtime tunable, default to making it a `ProxboxPluginSettings` field
(NetBox-UI-editable, persisted in the NetBox database) and read it via
`proxbox_api.runtime_settings.get_int / get_float / get_bool / get_str`, which already
resolves **env var (override) → `ProxboxPluginSettings` → built-in default** with a
5-minute settings cache (`proxbox_api/settings_client.py::get_settings`).

Only fall back to a pure `.env` variable when the value is needed **before** the NetBox
connection exists or is **operator-only infrastructure** that has no business in the UI:
`PROXBOX_BIND_HOST`, `PROXBOX_RATE_LIMIT`, `PROXBOX_ENCRYPTION_KEY` /
`PROXBOX_ENCRYPTION_KEY_FILE`, `PROXBOX_STRICT_STARTUP`,
`PROXBOX_SKIP_NETBOX_BOOTSTRAP`, `PROXBOX_GENERATED_DIR`,
`PROXBOX_CORS_EXTRA_ORIGINS`. Anything that controls sync behavior, batching,
concurrency, caching, or feature toggles belongs in `ProxboxPluginSettings`.

Do **not** invent shadow config layers (parallel JSON/YAML files, ad-hoc dotenv
sections, module-level constants meant as overrides) to dodge the migration cost.
If the new field needs the model + migration + form + serializer + template wiring on
the `netbox-proxbox` side, do all five — the existing fields in
`netbox_proxbox/models/plugin_settings.py` and migration
`0037_pluginsettings_runtime_tunables.py` show the pattern.

### Required in `.env` (process-level, no plugin-settings equivalent)

- `PROXBOX_BIND_HOST`: bind address used by the Docker `raw` and `granian` images (default: `0.0.0.0`). Set to `::` for IPv4 + IPv6 dual-stack. The container entrypoints sanitize surrounding ASCII quotes/whitespace, so a Compose list-form value such as `- PROXBOX_BIND_HOST="::"` is tolerated even though the YAML quotes are NOT stripped. The `nginx` image listens on both stacks regardless of this variable.
- `PROXBOX_RATE_LIMIT`: max API requests per minute per IP address (default: 60). Read at app construction.
- `PROXBOX_CORS_EXTRA_ORIGINS`: extra CORS origins (read at app construction).
- `PROXBOX_STRICT_STARTUP`: turns generated-route startup failures into fatal startup errors.
- `PROXBOX_SKIP_NETBOX_BOOTSTRAP`: skips default NetBox bootstrap at startup.
- `PROXBOX_GENERATED_DIR`: override output directory for the schema generator CLI (`proxbox-schema`); default is `$XDG_DATA_HOME/proxbox/generated/proxmox` (typically `~/.local/share/proxbox/generated/proxmox`).
- `PROXBOX_ENCRYPTION_KEY`: secret key used to encrypt credentials (NetBox token, Proxmox password/token) at rest in the local SQLite database. The raw value is hashed with SHA-256 to derive a Fernet key. Resolution order: env var > `ProxboxPluginSettings.encryption_key` (configurable from the NetBox plugin settings page) > local key file (default `<repo_root>/data/encryption.key`, managed via the `/admin/encryption/*` endpoints) > none. Startup never aborts; if no key is configured, credentials are stored in plaintext and a CRITICAL log is emitted on first encryption attempt.
- `PROXBOX_ENCRYPTION_KEY_FILE`: optional override for the local key file path used when neither the env var nor the plugin settings provide a key. Defaults to `<repo_root>/data/encryption.key`.
- `PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS`: legacy opt-in flag for plaintext credential storage. No longer required for startup; kept for compatibility with operators who scripted it.

### Plugin-managed (env override optional, defaults shown)

Each maps to a key in `ProxboxPluginSettings` and can be edited from the NetBox plugin settings page.

| Env var | Plugin key | Default |
|---------|-----------|---------|
| `PROXBOX_NETBOX_TIMEOUT` | `netbox_timeout` | 120 s |
| `PROXBOX_NETBOX_MAX_CONCURRENT` | `netbox_max_concurrent` | 1 |
| `PROXBOX_NETBOX_MAX_RETRIES` | `netbox_max_retries` | 5 |
| `PROXBOX_NETBOX_RETRY_DELAY` | `netbox_retry_delay` | 2.0 s |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `vm_sync_max_concurrency` | 4 |
| `PROXBOX_FETCH_MAX_CONCURRENCY` | `proxbox_fetch_max_concurrency` | 8 |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `proxmox_fetch_concurrency` | 8 (4 in task-history) |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `netbox_write_concurrency` | 8 (4 in task-history/snapshots) |
| `PROXBOX_BACKUP_BATCH_SIZE` | `backup_batch_size` | 5 |
| `PROXBOX_BACKUP_BATCH_DELAY_MS` | `backup_batch_delay_ms` | 200 ms |
| `PROXBOX_BULK_BATCH_SIZE` | `bulk_batch_size` | 50 |
| `PROXBOX_BULK_BATCH_DELAY_MS` | `bulk_batch_delay_ms` | 500 ms |
| `PROXBOX_NETBOX_GET_CACHE_TTL` | `netbox_get_cache_ttl` | 60 s (0 = disabled) |
| `PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES` | `netbox_get_cache_max_entries` | 4096 |
| `PROXBOX_NETBOX_GET_CACHE_MAX_BYTES` | `netbox_get_cache_max_bytes` | 52_428_800 (50 MB) |
| `PROXBOX_DEBUG_CACHE` | `debug_cache` | false |
| `PROXBOX_EXPOSE_INTERNAL_ERRORS` | `expose_internal_errors` | false |
| `PROXBOX_CUSTOM_FIELDS_REQUEST_DELAY` | `custom_fields_request_delay` | 0.0 s |

## Validation

Run these checks before pushing changes (the `rtk` prefix is a local token-saving alias around the underlying `uv run` commands; `uv run ruff check .` etc. are the canonical forms):

```bash
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall proxbox_api tests
uv run python -c "import proxbox_api.main"
uv run python -c "from proxbox_api.proxmox_to_netbox.proxmox_schema import load_proxmox_generated_openapi; assert load_proxmox_generated_openapi().get('paths')"
uv run ty check proxbox_api/types proxbox_api/utils/retry.py proxbox_api/schemas/sync.py
uv run pytest tests
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
