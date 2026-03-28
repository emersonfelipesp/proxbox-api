# Synchronization Workflows

This page explains major synchronization workflows between Proxmox and NetBox.

## Full update flow

HTTP endpoint:

- `GET /full-update`

High-level sequence:

1. Create NetBox sync-process record.
2. Sync Proxmox nodes into NetBox devices.
3. Sync Proxmox virtual machines into NetBox VMs.
4. Mark sync-process as completed and store runtime.

## Virtual machine sync flow

Primary endpoint:

- `GET /virtualization/virtual-machines/create`

Core behavior:

- Reads cluster resources from Proxmox sessions.
- Resolves VM configs per VM (`qemu`/`lxc`).
- Builds normalized NetBox payload.
- Creates dependencies (cluster, device, role) as needed.
- Creates VM interfaces and IP addresses when possible.
- Writes journal entries for auditability.

## Backup sync flow

Endpoints:

- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`

Core behavior:

- Discovers backup content in Proxmox storage.
- Maps backups to NetBox VMs.
- Creates backup objects under NetBox plugin model.
- Handles duplicate detection.
- Optional deletion of backups missing in Proxmox source.

## Tracking and observability

- Sync process records are created in NetBox plugin objects.
- Journal entries are written with summary and errors.
- WebSocket workflows provide interactive status output.

## Failure handling

- Domain errors are raised via `ProxboxException` and returned as structured JSON by app-level handler.
- Route handlers perform best-effort continuation in certain batch loops.
