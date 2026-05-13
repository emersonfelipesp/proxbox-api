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
| `factory.py` | `create_app()` — assembles the full FastAPI application: registers all routers, mounts static files, sets custom OpenAPI, wires exception handlers, and starts generated Proxmox route registration during lifespan. |
| `bootstrap.py` | Initializes SQLite tables, opens the default NetBox session, and records bootstrap status for health checks. Called once during lifespan startup. |
| `cors.py` | Builds CORS allowed-origin list from active NetBox endpoint records. |
| `exceptions.py` | Registers exception handlers that convert `ProxboxException` into structured HTTP error responses. |
| `cache_routes.py` | Cache control and invalidation API endpoints (`/cache/*`). |
| `websockets.py` | WebSocket connection manager — tracks active connections and broadcasts sync progress messages. |
| `full_update.py` | `POST /full-update` endpoint — orchestrates a full Proxmox-to-NetBox sync run with SSE or WebSocket streaming. Each handler registers its `operation_id` via `sync_state` so `GET /sync/active` reflects in-flight work. |
| `sync_state.py` | Process-local registry of in-flight sync runs. Exposes `register_active_sync` (async context manager), `acquire_active_sync` / `release_active_sync` (for non-`with` call sites), and `get_active_sync` / `is_active` for the `/sync/active` probe. |
| `root_meta.py` | Root metadata router — version, health, and standalone-mode info endpoints. |
| `netbox_session.py` | Helpers for retrieving the raw NetBox session outside of dependency injection. |
| `__init__.py` | Re-exports `create_app` for import convenience. |

## Application Startup Sequence

1. `create_app()` is called (imported by `proxbox_api.main`).
2. Lifespan starts: `bootstrap.py` initializes the database and default NetBox session.
3. Generated Proxmox routes are loaded and registered from `proxbox_api/generated/`.
4. Middleware (CORS, logging) and exception handlers are attached.
5. All routers from `proxbox_api/routes/` are mounted.
6. App is ready to serve.

## Key Rules

- Keep `factory.py` as the single composition root. Do not initialize sessions or routes elsewhere at module level.
- `bootstrap.py` is idempotent: calling it when the database already exists is safe.
- WebSocket broadcasts in `websockets.py` must tolerate disconnected clients silently.
- `PROXBOX_STRICT_STARTUP=1` turns generated-route load failures into fatal startup errors.
- `PROXBOX_SKIP_NETBOX_BOOTSTRAP=1` disables the default endpoint bootstrap (useful in test environments).
