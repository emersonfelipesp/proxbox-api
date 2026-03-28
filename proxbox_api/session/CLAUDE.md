# proxbox_api/session Directory Guide

## Purpose

Session management utilities for NetBox and Proxmox API clients.

## Modules and Responsibilities

- `netbox.py`: NetBox API session creation and dependency wiring.
- `proxmox.py`: Proxmox session management and dependency provider utilities.

## Key Data Flow and Dependencies

- netbox.py resolves endpoint credentials from the database and returns pynetbox sessions.
- proxmox.py builds ProxmoxAPI sessions and enriches them with cluster metadata.

## Extension Guidance

- Keep connection bootstrapping deterministic and avoid hidden global state when possible.
- Normalize upstream connection errors into ProxboxException.
