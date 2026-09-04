# proxbox_api/app Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/app/CLAUDE.md` inside the `personal-context` workspace.
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
| `bootstrap.py` | Resolves the guarded SQLite target, initializes its complete probe/schema boundary (including auth-lockout validation) under the target-specific interprocess lock, opens the default NetBox session, and records bootstrap status. Database failures are fatal while an absent NetBox endpoint remains non-fatal. |
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
2. Lifespan starts: `bootstrap.py` resolves one guarded absolute SQLite target; a persistent sibling lock serializes WAL/write proof, engines/tables, schema inspection, and every migration. The mandatory endpoint-table read then succeeds before optional NetBox client creation.
3. Generated Proxmox routes are loaded and registered from `proxbox_api/generated/`.
4. The NetBox bootstrap pass records `app.state.bootstrap_status`, which is exposed by `GET /extras/bootstrap-status`.
5. App becomes ready to serve; any database configuration/write failure prevents this transition.
6. Lifespan shutdown disposes the sync and async engines and clears process-local database handles.

## Key Rules

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
- `PROXBOX_SKIP_NETBOX_BOOTSTRAP=1` disables the default endpoint bootstrap (useful in test environments).
- Full-update is the sole owner of its task-history stage: both REST and SSE
  VM-stage calls pass `sync_task_history=False`, then invoke
  `sync_all_virtual_machine_task_histories()` once. Forward
  `fetch_max_concurrency` to that dedicated stage and do not re-enable the
  standalone VM default inside full-update.
