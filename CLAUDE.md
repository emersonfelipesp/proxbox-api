# proxbox-api Project Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Overview

`proxbox-api` is a FastAPI backend that connects Proxmox inventory and lifecycle data to NetBox objects. It serves REST, SSE, and WebSocket endpoints for discovery, synchronization, endpoint management, generated Proxmox proxy routes, and Firecracker host-agent provisioning for the NMS Cloud runtime. The same repository also includes a standalone `nextjs-ui/` frontend for endpoint administration.

### Companion repos (cross-link map)

- **`netbox-proxbox` v0.0.20** — the NetBox plugin that consumes this backend.
  Source: <https://github.com/emersonfelipesp/netbox-proxbox>. The current
  pairing is `netbox-proxbox 0.0.20` ↔ `proxbox-api 0.0.17` ↔ `proxmox-sdk 0.0.11`
  ↔ `netbox-sdk 0.0.8.post1`. Operational-verb routes (start/stop/snapshot/migrate)
  require `proxbox-api >= 0.0.17`; firewall model scaffolding and intent tag
  helpers require `>= 0.0.13`; HA tab and runtime tunables alone require `>= 0.0.11`.
  Firecracker Cloud uses the plugin for host pools, host-agent inventory, image
  templates, and `FirecrackerMicroVM` rows while this backend calls the selected
  host-agent through `/cloud/firecracker/*`.
- **Workspace note**:
  `personal-context/claude-reference/proxbox-api.md` (deep-dive index of this
  repo) and `personal-context/claude-reference/netbox-proxbox.md` (deep-dive
  index of the plugin) live in the AI workspace and should be kept in sync
  when route prefixes, env vars, or required dependency floors change.

## Use This Index First

Open the nearest scoped guide for the code you are changing.

### Top-level packages

- `proxbox_api/CLAUDE.md` — Core FastAPI package overview
- `proxbox-reconcile-rs/CLAUDE.md` — Optional Rust VM reconciliation engine
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
- `proxbox_api/services/sync/reconciliation/CLAUDE.md` — VM reconciliation seam and engine modes
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
- `proxbox-reconcile-rs/`: optional PyO3/maturin Rust package for VM operation-queue reconciliation parity testing and opt-in execution.
- `proxmox-sdk/`: Schema-driven Proxmox API package used for both mock endpoints and real API access.
- `nextjs-ui/`: Next.js frontend used to manage one NetBox endpoint and multiple Proxmox endpoints.
- `tests/`: Unit, integration, and end-to-end tests for the backend package.
- `benchmarks/`: local benchmark helpers, including VM reconciliation queue datasets and timers.
- `docs/`: MkDocs documentation, including English and Brazilian Portuguese content.
- `scripts/`: Utility scripts, including schema refresh helpers.
- `automation/`: Placeholder for future automation workflows.
- `tasks/`: Development task tracking.
- `Dockerfile` and `docker/`: runtime and reverse-proxy images for local and published deployments.
- `.github/workflows/`: CI/CD pipelines for test, lint, publish, and docs.
- `.gitea/workflows/mirror-github.yml`: Gitea Actions mirror from Gitea `main`
  to `github.com/emersonfelipesp/proxbox-api` using `GH_MIRROR_TOKEN` for
  GitHub, `SOURCE_MIRROR_TOKEN` for authenticated Gitea source fetches, the
  dedicated `mirror-host` runner label, `gh` authentication, and a single-branch
  `HEAD:refs/heads/main` push. Do not broaden it to tags, `--all`, or
  `--mirror`.
- `.gitea/workflows/deploy-production.yml`: Gitea Actions production deploy from
  `main` to `10.0.30.207` through the `prod-deploy` runner on the Gitea server
  (`10.0.30.96`). It uses the restricted SSH alias `nmc-prod-207` and the
  allowlisted `deploy proxbox-api <sha>` / `status proxbox-api` commands.
- `.gitea/workflows/publish-gitea.yml`: Gitea Package Registry publish workflow
  committed to `main`. Handles `push: tags:`, `create`, and `workflow_dispatch`
  events: builds dist, publishes to Gitea Package Registry (`PKG_TOKEN`), pushes
  tag to GitHub, and creates/publishes the GitHub release for non-RC tags (which
  fires `release: published` on GitHub Actions). Secret name: `PKG_TOKEN`
  (`GITEA_` prefix is reserved by Gitea Actions and cannot be used for secrets).
  If Gitea 1.26.2 tag triggers are not operational on this instance, use the
  manual fallback documented in the Release Procedure section below.

## Architecture

### Core layers

- API and app composition (`proxbox_api/app/*`, `proxbox_api/main.py`, `proxbox_api/routes/*`): create the FastAPI app, register routers, mount middleware, expose WebSocket and SSE streams, and keep request handlers thin.
- Firecracker host-agent layer (`proxbox_api/routes/cloud/firecracker.py`, `proxbox_api/firecracker_agent/`, `proxbox_api/schemas/firecracker.py`): validates Cloud provisioning payloads, calls host-agent health/capacity/assets/create/action endpoints, and emits the streaming progress contract consumed by `nms-backend`.
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
4. VM sync routes prepare Proxmox/NetBox state, then delegate deterministic VM
   operation-queue reconciliation to `proxbox_api.services.sync.reconciliation`.
5. Route handlers delegate remaining heavy work to service modules and schemas.
6. Firecracker Cloud routes under `/cloud/firecracker/*` call a selected host-agent VM after `nms-backend` resolves NetBox Proxbox inventory and creates the `FirecrackerMicroVM` row.
7. Sync and provisioning runs emit journal entries, structured logs, and optional WebSocket or SSE progress messages.

### Route Group Map

- **Proxmox operational verbs** (`proxbox_api/routes/proxmox_actions.py`, mounted at `/proxmox`):
  - `POST /proxmox/{vm_type}/{vmid}/start?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
  - `POST /proxmox/{vm_type}/{vmid}/stop?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
  - `POST /proxmox/{vm_type}/{vmid}/snapshot?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
  - `POST /proxmox/{vm_type}/{vmid}/migrate?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
  - `POST /proxmox/{vm_type}/{vmid}/reboot?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
  - `DELETE /proxmox/{vm_type}/{vmid}?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
  - `POST /proxmox/{vm_type}/{vmid}/backup?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
  - `DELETE /proxmox/{vm_type}/{vmid}/snapshot/{snapname}?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`

## Production Docker CI/CD

Production deploys are Gitea-first. A push to Gitea `main` triggers
`.gitea/workflows/deploy-production.yml`, which calls:

```bash
ssh nmc-prod-207 -- deploy proxbox-api "$GITHUB_SHA"
```

The production host is `10.0.30.207`. Deploy host state is kept outside the
repository under `/opt/nmulticloud/deploy`:

- Compose project: `nmc-proxbox-api`
- Repo checkout: `/opt/nmulticloud/deploy/repos/proxbox-api`
- Compose env: `/opt/nmulticloud/deploy/env/proxbox-api.compose.env`
- Runtime secrets: `/etc/nms/proxbox-api-production.env`
- SQLite state: `/opt/nmulticloud/deploy/state/proxbox-api/database.db`

The Docker runtime uses this repo's raw uvicorn image, host networking,
`PROXBOX_BIND_HOST=127.0.0.1`, `PORT=18800`, and `UVICORN_WORKERS=4`, matching
the old `proxbox-api-production.service` port and worker count while keeping
Nginx/TLS routing unchanged. Production mounts the state directory at
`/var/lib/proxbox-api` and sets
`PROXBOX_DATABASE_PATH=/var/lib/proxbox-api/database.db`.

Useful operations:

```bash
ssh nmc-prod-207 -- status proxbox-api
ssh nmc-prod-207 -- logs proxbox-api
ssh nmc-prod-207 -- health proxbox-api
curl -fsS http://127.0.0.1:18800/health
```

`proxbox-api-production.service` remains the rollback fallback. Do not start it
while the Docker container is healthy on port `18800`.

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
`PROXBOX_BIND_HOST`, `PROXBOX_DATABASE_PATH`, `PROXBOX_RATE_LIMIT`,
`PROXBOX_ENCRYPTION_KEY` / `PROXBOX_ENCRYPTION_KEY_FILE`, `PROXBOX_STRICT_STARTUP`,
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
- `PROXBOX_DATABASE_PATH`: optional SQLite database path override. Default is `/data/database.db` (a Docker volume mount point). Docker volumes should be mounted at `/data` to persist the database across container restarts and image upgrades. Production deployments can override this to `/var/lib/proxbox-api/database.db` if needed.
- `PROXBOX_RATE_LIMIT`: max API requests per minute per IP address (default: 300). Read at app construction.
- `PROXBOX_CORS_EXTRA_ORIGINS`: extra CORS origins (read at app construction).
- `PROXBOX_STRICT_STARTUP`: turns generated-route startup failures into fatal startup errors.
- `PROXBOX_SKIP_NETBOX_BOOTSTRAP`: skips default NetBox bootstrap at startup.
- `PROXBOX_GENERATED_DIR`: override output directory for the schema generator CLI (`proxbox-schema`); default is `$XDG_DATA_HOME/proxbox/generated/proxmox` (typically `~/.local/share/proxbox/generated/proxmox`).
- `PROXBOX_ENCRYPTION_KEY`: secret key used to encrypt credentials (NetBox token, Proxmox password/token) at rest in the local SQLite database. The raw value is hashed with SHA-256 to derive a Fernet key. Resolution order: env var > `ProxboxPluginSettings.encryption_key` (configurable from the NetBox plugin settings page) > local key file (default `<repo_root>/data/encryption.key`, managed via the `/admin/encryption/*` endpoints) > none. Startup never aborts; if no key is configured, credentials are stored in plaintext and a CRITICAL log is emitted on first encryption attempt.
- `PROXBOX_ENCRYPTION_KEY_FILE`: optional override for the local key file path used when neither the env var nor the plugin settings provide a key. Defaults to `<repo_root>/data/encryption.key`.
- `PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS`: legacy opt-in flag for plaintext credential storage. No longer required for startup; kept for compatibility with operators who scripted it.
- `PROXBOX_LOG_LEVEL`: console log verbosity (default `INFO`). Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (case-insensitive). Controls only the console handler; the in-memory buffer always receives DEBUG+ and the rotating file handler always writes WARNING+. Setting `DEBUG` also enables full `netbox_sdk.client` per-request tracing which is suppressed at all other levels to prevent INFO-level flooding.
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
| `PROXBOX_INTERFACE_BATCH_SIZE` | `interface_batch_size` | 5 |
| `PROXBOX_INTERFACE_BATCH_DELAY_MS` | `interface_batch_delay_ms` | 100 ms |
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

If you touch `proxbox_api/services/sync/reconciliation/`, `tests/reconciliation/`,
`benchmarks/reconciliation/`, `proxbox-reconcile-rs/`, or `.github/workflows/rust-reconcile.yml`,
also run the focused Rust/parity checks:

```bash
cargo test --no-default-features --manifest-path proxbox-reconcile-rs/Cargo.toml
uv pip install -e proxbox-reconcile-rs
PROXBOX_RECONCILIATION_ENGINE=compare \
  PROXBOX_RECONCILIATION_COMPARE_STRICT=true \
  uv run pytest tests/reconciliation -q
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

## Rust-Python FFI Reference

The backend now has one optional native extension:
`proxbox-reconcile-rs`, a PyO3/maturin Rust package for the deterministic VM
operation-queue builder used by full VM sync. Python remains the default engine
because live `netbox.nmulti.cloud` timing and synthetic benchmarks showed the
full Rust path was not faster after JSON/adaptation overhead.

The runtime seam is:

```
prepared_vms + netbox_snapshot + flags
  -> proxbox_api.services.sync.reconciliation.build_vm_operation_queue()
  -> Python engine, compare mode, or optional Rust engine
  -> CREATE | GET | UPDATE operations with patch_payload
```

Keep FastAPI routes, NetBox/Proxmox clients, SQLite, auth, retries, streaming,
and dispatch execution in Python. Only pure, synchronous, CPU-bound queue
construction belongs behind the Rust bridge.

Engine modes are selected through `ProxboxPluginSettings.reconciliation_engine`.
This selector is intentionally DB-backed through NetBox plugin settings, not a
backend environment-variable override.

- `reconciliation_engine=python`: default and production-safe path.
- `reconciliation_engine=compare`: run both engines, return Python,
  and report mismatches through logs and `proxbox_reconcile_mismatch_total`.
- `reconciliation_engine=rust`: return Rust output; requires the
  native package and should only be used after compare mode is clean.
- `reconciliation_compare_strict=true`: raise on mismatch in compare mode,
  intended for validation and local parity debugging.

When adding future native Rust extensions, use PyO3 as the binding framework:

- Architecture reference: `/root/personal-context/PYO3.md`
- Per-directory guidance: `/root/personal-context/pyo3/CLAUDE.md`
- Dashboard overview: `/pyo3` route on the personal-context app
- Build backend: maturin (preferred) or setuptools-rust
- Version: PyO3 v0.28.3, minimum Rust 1.83

Current Rust package:

- `proxbox-reconcile-rs/`: Rust VM reconciliation crate.
- `proxbox_api/services/sync/reconciliation/rust_bridge.py`: Pydantic v2
  JSON-byte adapter and optional native import.
- `proxbox_api/services/sync/reconciliation/vm_queue.py`: engine-neutral
  wrapper, Python fallback, compare mode, strict mode, mismatch diffing, and
  dataclass adaptation.
- `tests/reconciliation/`: Python contract, bridge, engine-mode, and
  Rust/Python parity fixtures.
- `.github/workflows/rust-reconcile.yml`: Rust unit tests, strict parity matrix,
  and wheel build matrix.

Candidate hotpaths for future Rust acceleration:
- `proxbox_api/proxmox_to_netbox/` — object mapping and field transformation
- `proxbox_api/proxmox_codegen/` — OpenAPI schema crawling and code generation
- `proxbox_api/services/sync/` — additional bulk reconciliation and diffing
- `proxbox_api/generated/` — generated Pydantic model validation

## Extension Rules

1. Update schemas and enums before route handlers.
2. Put reusable workflow logic in services, not routes.
3. Keep route modules focused on request orchestration and response shaping.
4. Add or update tests for new behavior.
5. Regenerate generated artifacts instead of editing them by hand.

## Branch Cleanup Policy

Always delete a feature branch (locally and on the remote) immediately after it
has been merged into its target branch. This applies to every branch — feature,
fix, security, chore, release-prep — and to merges done locally or via a pull
request.

After a merge:

1. Remove the task worktree first if one exists:
   `git worktree remove ../proxbox-api.worktrees/<slug>`.
2. Delete the local branch: `git branch -d <branch>` (use `-D` only if Git
   reports the branch as unmerged after you have confirmed it really is merged).
3. Delete the remote branch if it was ever pushed:
   `git push origin --delete <branch>`. If `git ls-remote --heads origin <branch>`
   returns nothing, the remote already has no copy and this step is a no-op.
4. Run `git fetch --prune` (or `git remote prune origin`) so stale
   `origin/<branch>` refs disappear from local listings.

Never leave merged branches lingering. The only branches that should persist
long-term are `main`, active release branches, and any branch the user has
explicitly asked to keep.

## Software Engineering Life Cycle Requirements

This section establishes project-wide quality standards derived from industry-standard software engineering practices. All changes must conform to these requirements before release.

### Requirements Traceability and Design Documentation

**Architectural Design:** The backend's architecture is documented across:
- **Route contracts** (`proxbox_api/routes/`, `.github/workflows/*.yml`) — API surface and CI/CD integration points
- **Service layers** (`proxbox_api/services/`) — subsystem decomposition and dependency definitions
- **Schema definitions** (`proxbox_api/schemas/`) — NetBox, Proxmox, and Firecracker payload contracts
- **Database models** (`proxbox_api/session/`, `proxbox_api/models/`) — state management and persistence

Changes to routes, services, or schemas MUST include an updated architecture note in the closest CLAUDE.md explaining:
- What interface or subsystem changed
- Why the change is necessary (traceability to an issue or feature)
- What downstream systems are affected (NetBox plugin, NMS frontend, Firecracker host-agents)
- Any breaking changes or version floor bumps

**Verification:** Before opening a PR, confirm:
1. Route contracts match their docstrings and `.openapi` metadata
2. All new schemas are documented in the nearest CLAUDE.md
3. Breaking changes to Proxmox/NetBox/Firecracker contracts are flagged in the PR description
4. The netbox-proxbox plugin compatibility floor is noted (version `X.Y.Z` or later required)

### Code Coverage and Quality Metrics

**Coverage Target:** Maintain ≥85% code coverage for the `proxbox_api/` package. Coverage is measured by `pytest-cov` and reported in CI.

**Coverage Reporting:**
- Local: `uv run pytest tests --cov=proxbox_api --cov-report=term-missing`
- CI: GitHub Actions enforces coverage thresholds; failing coverage blocks merge
- Exclusions: `proxbox_api/generated/` (auto-generated), database layer (SQLAlchemy), test fixtures

**Uncovered Code:** If code cannot be easily covered, document the rationale with an inline comment:
```python
try:
    ...
except ConnectionError:  # pragma: no cover - occurs only on network outage
    pass
```

### Testing and Regression Requirements

**Test Suite:** All changes must include unit and integration tests:
- **Unit tests** (`tests/test_*.py`) — verify individual routes, schemas, and services
- **Integration tests** (`tests/integration/`) — verify backend + NetBox + Proxmox workflows end-to-end
- **Regression tests** — add a test that would fail on pre-fix code before implementing any fix

**Regression Testing:** Before release, run:
```bash
uv run pytest tests/ --timeout=60 -v --cov=proxbox_api --cov-report=term-missing
uv run pytest tests/reconciliation -q  # if you changed sync reconciliation
```
This verifies that no previously passing test was broken by the change.

**E2E Validation:** Changes to VM sync, reconciliation, Firecracker provisioning, or NetBox integration must be validated against the full E2E Docker stack:
```bash
docker compose -f e2e/docker/docker-compose.yml up --build -d
bash e2e/docker/wait-for-stack.sh
bash e2e/docker/smoke.sh
```

### Static Analysis and Quality Gates

**Ruff (Linting & Formatting):**
```bash
uv run ruff check .          # Detect errors, style violations, unused imports
uv run ruff format --check . # Enforce code formatting
```
All violations block CI. Fix before pushing.

**Type Checking (Pyright strict):**
```bash
uv run ty check proxbox_api/types proxbox_api/utils/retry.py proxbox_api/schemas/sync.py
```
Type mismatches block merge. Use `# type: ignore` only with justification.

**Defect Categories Detected:**
- Undefined variables, imports, method/attribute access
- Unused imports and dead code
- Security: SQL injection, unsafe exec/eval, insecure deserialization
- Type mismatches (Pyright strict mode)
- Complexity and maintainability

**Pre-commit Enforcement:**
```bash
uv run python -m compileall proxbox_api tests
uv run ruff check . && uv run ruff format --check .
uv run py check proxbox_api/types proxbox_api/utils/retry.py proxbox_api/schemas/sync.py
uv run pytest tests --timeout=60
```
All checks MUST pass before committing.

### Configuration Control and Change Management

**Configuration Items:** The following are managed under strict change control:
- Backend version (`pyproject.toml` version, `proxbox_api/__init__.py` `__version__`)
- NetBox compatibility floor (`proxbox_api/constants.py` `MIN_NETBOX_VERSION`)
- Proxbox API contracts (route signatures, schema payloads, SSE/WebSocket events)
- Database schema and migrations (any model/SQLModel changes)
- Environment variable list (all new `.env` variables must be documented in CLAUDE.md)

**Change Control Process:**
1. **Before changing a configuration item**, post a comment on the related GitHub issue explaining the change and impact.
2. **After merging**, update the relevant CLAUDE.md file to document the new requirement or floor.
3. **Release notes** MUST include breaking changes (e.g., "requires NetBox ≥4.5.8").

**Version Management:** Follow PEP 440:
- Use `X.Y.ZrcN` for release candidates (TestPyPI validation only)
- Use `X.Y.Z` for official releases
- Use `X.Y.Z.postN` for bug-fix releases (never `twine --skip-existing`)

### Pre-Release Verification Checklist

**Before opening a release PR or tag, verify ALL of the following:**

- [ ] All requirements are implemented and verified in code
- [ ] Code passes pre-commit checklist (syntax, lint, type-check, tests)
- [ ] Coverage is ≥85% (`pytest-cov --cov-report=term-missing`)
- [ ] Regression testing passes (`pytest tests/ --timeout=60 -v`)
- [ ] E2E Docker stack validation is green (if touching sync/Firecracker/NetBox paths)
- [ ] Changelog (`docs/release-notes/version-X.Y.Z.md`) is complete
- [ ] Architecture documentation (CLAUDE.md files) is updated
- [ ] NetBox compatibility floor is documented (version `X.Y.Z` or later required)
- [ ] Proxbox API breaking changes (if any) are flagged for netbox-proxbox
- [ ] All CI checks are green (GitHub Actions)

**During release publishing**:

- [ ] Only use Gitea `push: tags: vX.Y.Z` or `gh release create` (never force-push tags)
- [ ] Monitor both Gitea Actions and GitHub Actions for successful publication
- [ ] Verify dist is live on PyPI and Docker Hub before declaring success
- [ ] Update netbox-proxbox compatibility floor if this release changes the API contract

---

## Release Procedure

The publish workflow (`.github/workflows/publish-testpypi.yml`) fires on `push: tags: v*` (RC and final), `release: published`, and `workflow_dispatch`. The **Gitea-first** pipeline (introduced in v0.0.16) uses `.gitea/workflows/publish-gitea.yml` to publish to the Gitea Package Registry, push the tag to GitHub, and create the GitHub release — which fires the `release: published` event and triggers the PyPI publish.

| Trigger | Use for | Publishes to |
|---------|---------|--------------|
| `push: tags: v*rc*` (plain Gitea tag push to Gitea mirrored to GitHub) | RC `vX.Y.ZrcN` | TestPyPI via GitHub Actions |
| `release: published` (created by `publish-gitea.yml`) | Final `vX.Y.Z` and `vX.Y.Z.postN` | PyPI via GitHub Actions |
| Docker Hub publish | Called after PyPI validation | Docker Hub (raw/nginx/granian images) |

### Gitea-first release flow (standard — vX.Y.Z)

1. **Bump versions** on the release branch: `pyproject.toml`, `uv.lock`. Local checks:
   ```bash
   uv run ruff check . && uv run python -m compileall proxbox_api tests && uv run pytest tests
   ```
2. **Merge to `main`** on Gitea (normal merge or PR merge). Verify:
   ```bash
   git log --oneline origin/main | head -5
   grep '^version' pyproject.toml
   ```
3. **Push annotated tag to Gitea:**
   ```bash
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push gitea vX.Y.Z
   ```
4. **Gitea Actions runs `.gitea/workflows/publish-gitea.yml`:**
   - Builds dist, publishes to Gitea Package Registry (`PKG_TOKEN` secret).
   - Pushes tag to GitHub. This fires `push: tags: v*` on GitHub Actions.
   - For non-RC tags: creates (or publishes draft) GitHub release, which fires `release: published`.
   - The PyPI idempotency check in `publish-pypi` handles the `release: published` re-trigger gracefully (skips upload if already on PyPI).
5. **Monitor both CI runs:**
   ```bash
   gh run list --repo emersonfelipesp/proxbox-api --event push --limit 3
   gh run list --repo emersonfelipesp/proxbox-api --event release --limit 3
   ```
6. **Verify dist is live on PyPI:**
   ```bash
   pip index versions proxbox-api
   ```
7. **Cleanup**: delete the release branch locally and on both remotes.

### RC flow (TestPyPI gate)

1. Push `vX.Y.ZrcN` tag to Gitea. `publish-gitea.yml` publishes to Gitea registry and pushes tag to GitHub.
2. GitHub Actions `push: tags: v*rc*` fires → publishes to TestPyPI → validates.
3. Fix-forward with `rcN+1` if anything fails.

### Manual fallback (if Gitea Actions unavailable)

If Gitea Actions tag triggers are not operational on this instance (Gitea 1.26.2 limitation — confirm with `git.nmulti.cloud` admin), use the following direct-upload path:

```bash
# Build and publish to Gitea registry directly
uv build
uv run --with twine twine upload \
  --repository-url https://git.nmulti.cloud/api/packages/emersonfelipesp/pypi \
  --username emersonfelipesp --password $PKG_TOKEN \
  --non-interactive dist/*

# Push tag directly to GitHub (fires push: tags: v* on GitHub Actions)
git push origin vX.Y.Z

# Watch the tag-push publish run
gh run watch <run-id> --repo emersonfelipesp/proxbox-api

# Then create the GitHub release manually
gh release create vX.Y.Z --repo emersonfelipesp/proxbox-api --title vX.Y.Z --generate-notes
# The release: published run will fire; the PyPI idempotency check will skip the upload (already done)
```

Note: `PKG_TOKEN` is the secret name for Gitea package uploads. The `GITEA_` prefix is reserved by Gitea Actions and cannot be used as a secret name.

### What was done for v0.0.16

- Bumped versions, merged to main on Gitea.
- Pushed tag `v0.0.16` to Gitea. `publish-gitea.yml` was present but Gitea 1.26.2 tag triggers were not fully operational at time of release.
- Manual fallback path was used: built dist locally, uploaded to Gitea registry directly, pushed tag to GitHub → GitHub Actions `push: tags: v*` fired → proxbox-api 0.0.16 published to PyPI.
- GitHub draft release `v0.0.16` was created in a prior session but left as Draft. One-time cleanup: `gh release edit v0.0.16 --repo emersonfelipesp/proxbox-api --draft=false`.
- `release: published` re-triggered the workflow; the new PyPI idempotency check (added in this PR) skips the upload cleanly.
- Paired plugin: `netbox-proxbox 0.0.19`.

### Don't

- Don't add `twine --skip-existing`. The `publish-pypi` job has a PyPI existence pre-check; fix forward with `.postN` per PEP 440 for new versions.
- Don't force-push a published tag. Tags on the remote are immutable.
- Don't create a GitHub release before Gitea Actions has pushed the tag — the release `--target` branch needs the tag commit reachable.
