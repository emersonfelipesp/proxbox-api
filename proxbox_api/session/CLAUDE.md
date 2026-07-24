# proxbox_api/session Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/session/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Session management utilities for NetBox and Proxmox API clients.

## Current Modules

- `netbox.py`: NetBox API session creation and dependency wiring.
  - `get_netbox_session()`: async provider that resolves endpoint credentials from the SQLite database and returns a lifecycle-owned `netbox-sdk` `Api` facade for startup/bootstrap helpers and direct tests.
  - `get_netbox_session_sync()`: compatibility wrapper exported as top-level `proxbox_api.get_netbox_session`; it preserves the historical synchronous Python API outside a running event loop.
  - `netbox_config_from_endpoint()`: builds a `netbox_sdk.Config` from the stored `NetBoxEndpoint` record, including token v1/v2 support, and applies `PROXBOX_NETBOX_TIMEOUT`.
  - `get_netbox_async_session()`: async dependency entrypoint for FastAPI routes; it tolerates both `AsyncSession` runtime usage and sync SQLModel test sessions.
  - `netbox_api_from_endpoint()`: returns the single current client fingerprint for a persisted endpoint, using an exact structured configuration hash, rotating a changed configuration, and closing the retired transport outside the lifecycle lock. Unsaved rows are rejected because they have no stable cache identity.
  - `invalidate_netbox_api_cache()` / `close_netbox_api_cache()`: await targeted retirement or process-wide shutdown drain; repeated calls are safe.
  - `NetBoxSessionDep` / `NetBoxAsyncSessionDep`: FastAPI dependency aliases, typed `Annotated[Api, Depends(...)]` (the concrete `netbox-sdk` facade returned by the providers), so route handlers that inject a session get a checked `Api` type instead of `object`. The typed writer facade in `services/netbox_writers.py` mirrors this — its `upsert_*` helpers take `nb: Api` (imported under `TYPE_CHECKING`). Annotate new session-consuming params as `Api`, not bare `object`.
- `proxmox.py`: Proxmox session management module that re-exports the session types and helper functions.
- `proxmox_core.py`: shared Proxmox client core helpers.
- `proxmox_providers.py`: dependency helpers that resolve `ProxmoxSession` instances from DB or NetBox plugin endpoints. DB-source transport settings (timeout/retry/backoff) are fetched under a bounded wall-clock budget (`_DB_SETTINGS_REQUEST_TIMEOUT_SECONDS`, 0.5 s via `asyncio.timeout`) with per-event-loop single-flight sharing (`_DB_SETTINGS_INFLIGHT`); on timeout or failure the deterministic defaults apply so endpoint loading never blocks on settings. `enc:`-prefixed DB secrets that cannot be decrypted raise a clear `ProxboxException` (HTTP 503) instead of silently passing ciphertext, and credential parsing reuses the single bounded settings result rather than starting a second fetch. NetBox-source endpoint-ID filters are sent as repeated `id=` values in chunks of at most 100 (`_chunk_endpoint_ids`), matching NetBox's `MultiValueNumberFilter` contract. The related `proxbox_api/settings_client.py::get_settings` now single-flights concurrent cold fetches behind a `threading.Condition` with an explicit per-call `request_timeout_seconds` deadline and `cache_fallback` opt-out, so a bounded caller can never block on another caller's slower settings fetch.

## How These Sessions Flow

- `netbox.py` is the source of truth for building NetBox client sessions from persisted endpoint records.
- Its lifecycle owner is the only allowed process-wide NetBox client cache. It keeps one current entry per persisted endpoint id, uses revision checks to prevent a client retired by concurrent invalidation from being returned, tracks detached close tasks through completion, and never awaits while holding its lock.
- Endpoint create/update publishes an enabled committed row immediately. Disable/delete clears the default, explicit disabled IDs fail closed, and partial updates never re-encrypt an omitted credential's ciphertext.
- `proxmox_core.py` and `proxmox_providers.py` create proxmox-sdk async SDK sessions and enrich them with cluster metadata used by API request flows.
- `proxmox_providers.py` validates `endpoint_ids` before filtering which Proxmox endpoints participate in a request.

## Extension Guidance

- Keep connection bootstrapping deterministic; any process-wide NetBox state must remain explicit and owned by the lifecycle manager.
- Terminal shutdown rejects new acquisition and delays cancellation propagation until tracked retirements finish or hit the 10-second per-client close deadline. The deadline preserves endpoint-mutation and shutdown liveness when an SDK transport close stalls.
- Close failures must not log credentials, endpoint URLs, configuration fingerprints, or exception messages that can embed secret data.
- Normalize upstream connection errors into `ProxboxException` without retaining or chaining raw exception messages that may contain credentials.
- Keep dependency aliases in this package rather than duplicating them in route modules.
- When adjusting NetBox client timeouts, update the root docs and any setup documentation that mentions the environment variable.
