# proxbox_api/session Directory Guide

## Purpose

Session management utilities for NetBox and Proxmox API clients.

## Current Modules

- `netbox.py`: NetBox API session creation and dependency wiring.
  - `get_netbox_session()`: Resolves endpoint credentials from the SQLite database and returns a `SyncProxy`-wrapped `NetBoxApiClient` session.
  - `netbox_config_from_endpoint()`: Builds a `netbox_sdk.Config` from the stored `NetBoxEndpoint` record and applies `PROXBOX_NETBOX_TIMEOUT` to the SDK timeout.
  - `NetBoxSessionDep` / `NetBoxAsyncSessionDep`: FastAPI dependency aliases for sync and async NetBox sessions.
- `proxmox.py`: Proxmox session management and dependency provider utilities.
- `proxmox_core.py`: Shared Proxmox client core helpers.
- `proxmox_providers.py`: Dependency helpers that resolve `ProxmoxSession` instances from DB or NetBox plugin endpoints. Non-empty `endpoint_ids` must be a comma-separated list of integers; otherwise `ProxboxException` is raised.

## Key Data Flow and Dependencies

- `netbox.py` resolves endpoint credentials from the database and returns netbox-sdk sessions.
- `proxmox.py` builds ProxmoxAPI sessions and enriches them with cluster metadata.
- `proxmox_providers.py` validates `endpoint_ids` before filtering which Proxmox endpoints participate in a request.

## Extension Guidance

- Keep connection bootstrapping deterministic and avoid hidden global state when possible.
- Normalize upstream connection errors into `ProxboxException`.
- When adjusting NetBox client timeouts, update the `PROXBOX_NETBOX_TIMEOUT` env var documentation in the root guides and the mkdocs configuration guide.
