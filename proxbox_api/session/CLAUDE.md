# proxbox_api/session Directory Guide

## Purpose

Session management utilities for NetBox and Proxmox API clients.

## Modules and Responsibilities

- `netbox.py`: NetBox API session creation and dependency wiring.
  - `get_netbox_session()`: resolves endpoint credentials from the SQLite database and returns a `SyncProxy`-wrapped `NetBoxApiClient` session.
  - `netbox_config_from_endpoint()`: builds a `netbox_sdk.Config` from the stored `NetBoxEndpoint` record. Applies the `PROXBOX_NETBOX_TIMEOUT` environment variable (default: 120 seconds) to the SDK timeout.
  - `NetBoxSessionDep` / `NetBoxAsyncSessionDep`: FastAPI dependency aliases for sync and async NetBox sessions.
- `proxmox.py`: Proxmox session management and dependency provider utilities.

## Key Data Flow and Dependencies

- netbox.py resolves endpoint credentials from the database and returns netbox-sdk sessions.
- proxmox.py builds ProxmoxAPI sessions and enriches them with cluster metadata.

## Extension Guidance

- Keep connection bootstrapping deterministic and avoid hidden global state when possible.
- Normalize upstream connection errors into ProxboxException.
- When adjusting NetBox client timeouts, update the `PROXBOX_NETBOX_TIMEOUT` env var documentation in `CLAUDE.md` and the mkdocs configuration guide.
