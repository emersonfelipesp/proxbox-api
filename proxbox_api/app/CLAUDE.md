# proxbox_api/app Directory Guide

## Workspace Context

This file lives at `<repository-root>/proxbox_api/app/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Application factory and lifecycle management for the `proxbox-api` FastAPI service. This directory owns app composition, startup/shutdown, middleware, exception wiring, WebSocket management, and the full-update orchestration endpoint.

## Files

| File | Role |
|------|------|
| `factory.py` | `create_app()` — assembles the import-safe FastAPI application: validates auth-lockout policy/trusted-proxy process configuration, registers middleware/routers, mounts static files, sets custom OpenAPI, wires exception handlers, and starts database/bootstrap, adjacent HMAC-key validation, plus generated Proxmox route registration during lifespan. |
| `bootstrap.py` | Resolves the guarded SQLite target, initializes its complete probe/schema boundary (including auth-lockout validation) under the target-specific interprocess lock, opens the default NetBox session, and records bootstrap status. A condition-protected generation claim ensures simultaneous lifespans publish these process-global results exactly once. Database failures are fatal and shared with waiting owners, while an absent NetBox endpoint remains non-fatal. A `CephProviderTaskClaimMigrationError` raised during schema initialization is fatal: bootstrap records one stable reason and refuses startup. |
| `cors.py` | Builds CORS allowed-origin lists from active NetBox endpoint records, including endpoint rows loaded after app construction. |
| `exceptions.py` | Registers exception handlers that convert `ProxboxException` into structured HTTP error responses. |
| `cache_routes.py` | Cache control and invalidation API endpoints (`/cache/*`, `/clear-cache`), including durable label-free authentication lockout metrics plus NetBox GET cache invalidation. |
| `websockets.py` | WebSocket connection manager — uses the same normalized source/trust context and shared auth lockout service as HTTP, tracks active connections, and broadcasts sync progress messages. |
| `full_update.py` | `POST /full-update` endpoint — orchestrates a full Proxmox-to-NetBox sync run with SSE or WebSocket streaming. Each handler registers its `operation_id` via `sync_state` so `GET /sync/active` reflects in-flight work. |
| `sync_state.py` | Process-local registry of in-flight sync runs. Exposes `register_active_sync` (async context manager), `acquire_active_sync` / `release_active_sync` (for non-`with` call sites), and `get_active_sync` / `is_active` for the `/sync/active` probe. |
| `root_meta.py` | Root metadata router — version, health, and standalone-mode info endpoints. |
| `netbox_session.py` | Helpers for retrieving the raw NetBox session outside of dependency injection. |
| `__init__.py` | Re-exports `create_app` for import convenience. |

## Application Startup Sequence

1. `create_app()` is called (imported by `proxbox_api.main`) and assembles middleware, exception handlers, and routers without touching the database.
2. Lifespan starts by acquiring an opaque generation-bound owner token for the process-shared database runtime. The first owner initializes the guarded absolute SQLite target; later same-target owners register without rerunning schema or lockout-identity initialization. `bootstrap.py` publishes endpoint and NetBox globals once for that generation, and simultaneous owners wait for the same success or fatal failure. A persistent sibling lock serializes WAL/write proof, engines/tables, schema inspection, and every migration. A conflicting startup cannot release or mutate an incumbent owner's runtime.
3. Legacy user-generated Python models and unprovenanced route caches are quarantined, then generated Proxmox routes are loaded from immutable bundled schemas and registered. Provenance-verified user schemas are considered only when the development-only `PROXBOX_RUNTIME_CODEGEN_ENABLED=true` process opt-in was set before application construction.
4. The NetBox bootstrap pass records `app.state.bootstrap_status`, which is exposed by `GET /extras/bootstrap-status`.
5. App becomes ready to serve; any database configuration/write failure prevents this transition.
6. Lifespan shutdown releases only its own token. The final owner enters a condition-protected transition, detaches the process globals, and attempts both engine disposals despite repeated caller cancellation. Only complete cleanup releases the runtime lease and clears the lockout identity. A disposal failure pins both boundaries and poisons database reuse until process restart. Cleanup failures are propagated on normal shutdown but recorded without replacing an earlier startup or application failure. New owners wait for active transitions, offline maintenance remains excluded by the lease, and overlapping owners continue serving without interruption. The shared async engine is unpooled so distinct lifespan event loops cannot inherit each other's pooled connections. Condition waiters and the cleanup work that wakes them execute on separate dedicated executors and never depend on default-executor capacity.

## Key Rules

- The outer interactive ASGI middleware owns guarded lifetimes before FastAPI
  dependencies. Synchronization WebSocket route-level authentication must stay
  ahead of the complete provider graph; mounted tests pin this solver ordering.
  Quiesce the local interactive runtime before database disposal. The local
  authenticated status route must not acquire managed providers or certify peers.

- Keep `factory.py` as the single composition root. Do not initialize sessions or routes elsewhere at module level.
- Preserve the transport peer in the ASGI scope: Uvicorn/FastAPI entrypoints
  must disable their proxy-header rewriting. `factory.py` alone applies the
  validated `PROXBOX_TRUSTED_PROXIES` policy before lockout and rate limiting.
- `bootstrap.py` is idempotent for the same configured target. A second, conflicting target in one process is an error.
- Database target resolution and verification must remain before route/bootstrap work that can accept traffic. Never catch and downgrade `DatabaseConfigurationError` or `DatabaseStartupError`.
- Keep probe, engine/table creation, and all migrations inside the same target-specific advisory-lock acquisition. The lock file is persistent and must not be unlinked while workers can run.
- Never downgrade migration inspection or the required post-schema endpoint-table read to an optional NetBox connection failure.
- WebSocket broadcasts in `websockets.py` must tolerate disconnected clients silently.
- `PROXBOX_STRICT_STARTUP=1` turns generated-route load failures into fatal startup errors.
- `PROXBOX_RUNTIME_CODEGEN_ENABLED=true` is a development-only process opt-in
  that mounts the HTTP generation and refresh routes and permits user-schema
  discovery. Production leaves it unset so startup and source rendering use
  bundled schemas only.
- `PROXBOX_SKIP_NETBOX_BOOTSTRAP=1` disables the default endpoint bootstrap (useful in test environments).
- Each application lifespan owns the shared NetBox client cache. Endpoint mutation retires obsolete SDK generations without disrupting existing borrowers; shutdown releases ownership and atomically detaches current and retired clients only after the final overlapping lifespan exits. NetBox-client and database-runtime releases are attempted independently, so repeated cancellation or failure in one cannot skip the other; cleanup failures preserve an active primary lifespan error.
- Full-update is the sole owner of its task-history stage: both REST and SSE
  VM-stage calls pass `sync_task_history=False`, then invoke
  `sync_all_virtual_machine_task_histories()` once. Forward
  `fetch_max_concurrency` to that dedicated stage and do not re-enable the
  standalone VM default inside full-update.
