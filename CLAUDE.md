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

- **`netbox-proxbox` v0.0.18** — the NetBox plugin that consumes this backend.
  Source: <https://github.com/emersonfelipesp/netbox-proxbox>. The v0.0.18 release
  (PVE 9.2 SDN models, CPU model, HA arm/disarm views, node-level firewall sync)
  requires `proxbox-api >= 0.0.14`; firewall model scaffolding and intent tag
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

## Release Procedure

The publish workflow (`.github/workflows/publish-testpypi.yml`) fires on
**both** `push: tags: v*` and `release: types: [published]`. That means a
single non-rc tag is enough to trigger PyPI publish — and creating a GitHub
release **after** the tag spawns a *duplicate* publish run that must be
cancelled to avoid wasted CI and to keep the run history clean.

### Standard release flow (used for `v0.0.12`)

1. **Land the release on the release branch.** Bump
   `pyproject.toml`, `proxbox_api/__init__.py`, and any other version
   references; merge into `main` (or the active release branch) with a
   normal merge commit. No `--no-ff` is required, but never force-push.
2. **Annotated tag.** From a clean checkout of the release commit:
   ```bash
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin vX.Y.Z
   ```
   The tag push triggers the publish workflow. Watch it to completion:
   ```bash
   gh run watch <run-id> --repo emersonfelipesp/proxbox-api
   ```
3. **Verify the dist is live on PyPI** before doing anything else:
   ```bash
   curl -s https://pypi.org/pypi/proxbox-api/json | jq '.releases | keys'
   ```
4. **Create the GitHub release** so the tag has notes and shows up in the
   project's release listing:
   ```bash
   gh release create vX.Y.Z \
     --repo emersonfelipesp/proxbox-api \
     --title vX.Y.Z \
     --generate-notes
   ```
   `--generate-notes` auto-builds release notes from PRs/commits since the
   previous release.
5. **Cancel the duplicate publish run that the release just spawned.**
   `release: published` re-fires the publish workflow against the same tag.
   The dist already exists on PyPI so the upload step would fail anyway, but
   the run still spends CI minutes and clutters the actions tab. Right after
   `gh release create` returns:
   ```bash
   gh run list --repo emersonfelipesp/proxbox-api --event release --limit 5 \
     --json databaseId,name,status
   # cancel every in_progress run from that listing
   gh run cancel <run-id> --repo emersonfelipesp/proxbox-api
   ```
   On a typical proxbox-api release the duplicate runs are
   `Release validation and publish`, `Release Docker verification`, and
   `CI`. Always cancel them — `Release Docker verification` is also wasted
   because the Docker image is built and pushed from the tag-event run, not
   the release-event run.
6. **Branch cleanup** per the Branch Cleanup Policy above. For non-`main`
   release branches (e.g. `v0.0.12`), once PyPI is green, delete the branch
   locally and on the remote so only `main` and `gh-pages` persist.

### What was done for v0.0.12

- Merged the v0.0.12 release line into `main` (final commit `828a2f6`).
- Pushed annotated tag `v0.0.12`. Tag-event publish run completed green:
  PyPI dist `proxbox-api 0.0.12` verified.
- Created GitHub release with `gh release create v0.0.12 --repo
  emersonfelipesp/proxbox-api --title v0.0.12 --generate-notes`.
- That spawned three release-event runs which were cancelled with
  `gh run cancel`: `Release validation and publish`,
  `Release Docker verification`, and `CI`.
- Deleted the `v0.0.12` release branch locally and on the remote via
  `git push origin :refs/heads/v0.0.12` (explicit refspec is required when
  a branch and a tag share the same name, otherwise Git complains that
  `v0.0.12 matches more than one`).
- After cleanup, only `main` and `gh-pages` remain on origin.

### What was done for v0.0.13

- Bumped `pyproject.toml` and `proxbox_api/__init__.py` to `0.0.13` (final merge commit `e64cebb`).
- Bumped `proxmox-sdk` dependency from `0.0.4.post2` → `0.0.5.post1`.
- New features: read-only Proxmox VE firewall routes (`/proxmox/firewall/*`
  covering datacenter, node, and per-VM QEMU/LXC zones), intent tag helpers
  (`PUT /intent/tag-pending-deletion`, `PUT /intent/untag-pending-deletion`),
  cloud provision SSE stream (`POST /cloud/vm/provision/stream`).
- Bug fixes: `resolve_vm_config` cluster-status preflight removed (#134),
  dual-stack primary IP sync to `primary_ip4`/`primary_ip6` (#123),
  FastAPI `run_id` default leak (#132), bootstrap skip when no NetBox
  endpoint configured (#130), concurrent tag-creation race recovery (#124),
  `PROXBOX_LOG_LEVEL` env var for console verbosity (#133).
- Added NetBox v4.6.1 to `.github/netbox-versions.json` certified versions.
- Paired consumer: `netbox-proxbox v0.0.17`.

### What was done for v0.0.14

- Bumped `pyproject.toml` to `0.0.14`.
- Bumped `proxmox-sdk` dependency from `0.0.5.post1` → `0.0.6` (PVE 9.2 schema, 675 operations / 449 endpoints, up from 646/389 in 9.1.11). Also bumped `proxmox-sdk[pbs]` and `proxmox-sdk[pdm]` optional extras.
- New routes — **HA (PVE 9.2)**:
  - `POST /proxmox/cluster/ha/disarm` — disarm HA stack cluster-wide (maintenance mode).
  - `POST /proxmox/cluster/ha/arm` — re-arm HA stack.
  - `GET /proxmox/cluster/ha/manager-status` — live HA CRM manager status.
  - `GET /proxmox/cluster/ha/crs` — Cluster Resource Scheduler configuration extracted from datacenter options.
- New routes — **SDN fabrics (PVE 9.2)**:
  - `GET /proxmox/sdn/fabrics` — list SDN fabrics (WireGuard, BGP, VXLAN, OSPF types).
  - `GET /proxmox/sdn/fabrics/all` — list all SDN fabrics including inherited ones.
  - `GET /proxmox/sdn/route-maps` — SDN route-map objects.
  - `GET /proxmox/sdn/prefix-lists` — SDN prefix-list objects.
- New routes — **Datacenter (PVE 9.2)**:
  - `GET /proxmox/datacenter/cpu-models` — list custom CPU models.
  - `GET /proxmox/datacenter/cpu-models/{cputype}` — get a single custom CPU model.
  - `POST /proxmox/datacenter/cpu-models` — create a custom CPU model.
  - `PUT /proxmox/datacenter/cpu-models/{cputype}` — update a custom CPU model.
  - `DELETE /proxmox/datacenter/cpu-models/{cputype}` — delete a custom CPU model.
  - `GET /proxmox/datacenter/options` — datacenter options including CRS sub-object and `location` field.
- New routes — **Access tokens (PVE 9.2)**:
  - `GET /proxmox/access/tokens/{userid}/{tokenid}` — read API token info.
  - `PUT /proxmox/access/tokens/{userid}/{tokenid}/regenerate` — regenerate token secret in-place.
- Extended routes — **Nodes (PVE 9.2)**:
  - `GET /proxmox/nodes/{node}/storage/{storage}/identity` — PBS storage instance ID.
  - `GET /proxmox/nodes/{node}/config` — node configuration including new `location` field.
- Tracked in issue: <https://github.com/emersonfelipesp/proxbox-api/issues/152>.
- Paired consumer: `netbox-proxbox v0.0.18`.

### Don't

- Don't add `twine --skip-existing` to the upload step. If a version is
  consumed but later fails validation, **fix forward** with the next
  `.postN` (PEP 440) — `0.0.12.post1`, `0.0.12.post2`, etc. The same
  fix-forward rule applies to rc tags (`rcN` → `rcN+1`).
- Don't force-push a release branch to "rewrite history" of a published
  tag. Tags on the remote are immutable; treat them as such.
- Don't skip step 5. Even though the duplicate publish run will fail at
  upload time (file already exists), leaving it `in_progress` for ~10
  minutes wastes runners and makes future "did the release publish?"
  diagnostics harder.
