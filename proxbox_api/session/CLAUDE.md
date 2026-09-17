# proxbox_api/session Directory Guide

## Workspace Context

This file lives at `<repository-root>/proxbox_api/session/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Session management utilities for NetBox and Proxmox API clients.

## Current Modules

- `netbox.py`: NetBox API session creation and dependency wiring.
  - `get_netbox_session()`: resolves endpoint credentials from the SQLite database and returns a `netbox-sdk` `Api` facade for explicit sync callers such as startup/bootstrap helpers and direct tests.
  - `netbox_config_from_endpoint()`: builds a `netbox_sdk.Config` from the stored `NetBoxEndpoint` record, including token v1/v2 support, and applies `PROXBOX_NETBOX_TIMEOUT`.
  - `get_netbox_async_session()`: async dependency entrypoint for FastAPI routes; it tolerates both `AsyncSession` runtime usage and sync SQLModel test sessions.
  - NetBox `Api` facades are cached by endpoint ID and credential/configuration fingerprint. Endpoint mutation atomically retires matching entries from new lookup but does not close transports that an in-flight request may still borrow. The final application lifespan owner atomically detaches current and retired clients, finishes `api.client.close()` outside the cache lock through repeated cancellation, and retains failed closures for retry; overlapping or newly arriving lifespans cannot lose clients to an earlier owner's drain.
  - `NetBoxSessionDep` / `NetBoxAsyncSessionDep`: FastAPI dependency aliases, typed `Annotated[Api, Depends(...)]` (the concrete `netbox-sdk` facade returned by the providers), so route handlers that inject a session get a checked `Api` type instead of `object`. The typed writer facade in `services/netbox_writers.py` mirrors this — its `upsert_*` helpers take `nb: Api` (imported under `TYPE_CHECKING`). Annotate new session-consuming params as `Api`, not bare `object`.
  - The `Api` facade exposes transport configuration through `api.client.config`, not `api.config`. Keep reachability checks in an exercised caller with an explicit response contract; do not add unused session-layer probes that swallow failures into ad hoc dictionaries.
- `../netbox_probe.py`: request-private 10-second authenticated status probe with a typed response contract and separately bounded one-second temporary-client cleanup. It caches the latest result for 30 seconds by an unambiguous URL/TLS/credential fingerprint in a bounded, atomically replaced, `flock`-protected file beside the configured SQLite database, making results visible across workers without storing credentials. Both bootstrap and tag sync dependencies reject only a fresh known-unreachable result. Probe errors redact stored token material; repeated cancellation cannot interrupt cleanup before its deadline, and a stalled close is cancelled without indefinitely blocking the request.
- `proxmox.py`: Proxmox session management module that re-exports the session types and helper functions.
- `proxmox_core.py`: shared Proxmox client core helpers.
- `proxmox_providers.py`: dependency helpers that resolve `ProxmoxSession` instances from DB or NetBox plugin endpoints. DB-source transport settings (timeout/retry/backoff) are fetched under a bounded wall-clock budget (`_DB_SETTINGS_REQUEST_TIMEOUT_SECONDS`, 0.5 s via `asyncio.timeout`) with per-event-loop single-flight sharing (`_DB_SETTINGS_INFLIGHT`); on timeout or failure the deterministic defaults apply so endpoint loading never blocks on settings. `enc:`-prefixed DB secrets that cannot be decrypted raise a clear `ProxboxException` (HTTP 503) instead of silently passing ciphertext, and credential parsing reuses the single bounded settings result rather than starting a second fetch. NetBox-source endpoint-ID filters are sent as repeated `id=` values in chunks of at most 100 (`_chunk_endpoint_ids`), matching NetBox's `MultiValueNumberFilter` contract. The related `proxbox_api/settings_client.py::get_settings` now single-flights concurrent cold fetches behind a `threading.Condition` with an explicit per-call `request_timeout_seconds` deadline and `cache_fallback` opt-out, so a bounded caller can never block on another caller's slower settings fetch.

## How These Sessions Flow

- `netbox.py` is the source of truth for building NetBox client sessions from persisted endpoint records.
- `proxmox_core.py` and `proxmox_providers.py` create proxmox-sdk async SDK sessions and enrich them with cluster metadata used by API request flows.
- `proxmox_providers.py` validates `endpoint_ids` before filtering which Proxmox endpoints participate in a request.
- `ProxmoxSession._describe_auth_error` turns SDK connection failures into secret-safe operator detail. proxmox-sdk (>= 0.0.15) refuses every HTTP 3xx before reading a body (`ProxmoxRedirectError`), so an endpoint that sits behind a redirecting proxy fails with the refused status and target host plus the instruction to configure the final Proxmox API address; `ResourceException` keeps its HTTP status and structured error field names, and nothing else from the provider body is exposed.

## Extension Guidance

- Keep connection bootstrapping deterministic and avoid hidden global state.
- Normalize upstream connection errors into `ProxboxException`.
- Never pass raw SDK exception objects or their text to loggers. Emit a fixed,
  redacted error class/status diagnostic, and keep the handler-level
  `SensitiveDataFilter` effective for deferred string, mapping, and exception
  arguments so access keys, private keys, URLs, and provider response bodies
  cannot be rendered after the call site.
- Preserve structured connection details for callers while setting
  `redact_log_details=True` for session-created exceptions so raw SDK error text
  never enters constructor debug logs; owned log sites should emit error types only.
- `ProxmoxSession.create()` owns every SDK client acquired during
  initialization. Any `BaseException` from authentication or post-connect
  metadata discovery must trigger one shielded `aclose()` before the original
  failure is re-raised. Clear SDK ownership before invoking `close()` so cleanup
  failure, cancellation, or repeated cleanup cannot dispatch a second close;
  cleanup logs may contain only the exception type.
- Keep dependency aliases in this package rather than duplicating them in route modules.
- Never delete a cached NetBox facade without either retaining it in `_RETIRED_APIS` or closing its client. Endpoint mutation moves entries to retired state under `_API_CACHE_LOCK`; final-lifespan shutdown detaches both current and retired entries, performs async closure only after releasing the lock, retains failed closures for retry, and limits diagnostics to exception types without endpoint or credential values.
- When adjusting NetBox client timeouts, update the root docs and any setup documentation that mentions the environment variable.
