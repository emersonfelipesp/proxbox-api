# proxbox_api/session Directory Guide

## Purpose

Session management utilities for NetBox and Proxmox API clients.

## Current Modules

- `netbox.py`: NetBox API session creation and dependency wiring.
  - `get_netbox_session()`: resolves endpoint credentials from the SQLite database and returns a `SyncProxy`-wrapped `NetBoxApiClient` session.
  - `netbox_config_from_endpoint()`: builds a `netbox_sdk.Config` from the stored `NetBoxEndpoint` record and applies `PROXBOX_NETBOX_TIMEOUT`.
  - `NetBoxSessionDep` / `NetBoxAsyncSessionDep`: FastAPI dependency aliases for sync and async NetBox sessions.
- `proxmox.py`: Proxmox session management and dependency provider utilities.
- `proxmox_core.py`: shared Proxmox client core helpers.
- `proxmox_providers.py`: dependency helpers that resolve `ProxmoxSession` instances from DB or NetBox plugin endpoints.

## How These Sessions Flow

- `netbox.py` is the source of truth for building NetBox client sessions from persisted endpoint records.
- `proxmox.py` creates ProxmoxAPI sessions and enriches them with cluster metadata used by sync flows.
- `proxmox_providers.py` validates `endpoint_ids` before filtering which Proxmox endpoints participate in a request.

## Extension Guidance

- Keep connection bootstrapping deterministic and avoid hidden global state.
- Normalize upstream connection errors into `ProxboxException`.
- Keep dependency aliases in this package rather than duplicating them in route modules.
- When adjusting NetBox client timeouts, update the root docs and any setup documentation that mentions the environment variable.
