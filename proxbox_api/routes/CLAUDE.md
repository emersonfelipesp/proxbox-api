# proxbox_api/routes Directory Guide

## Purpose

Top-level namespace for FastAPI route packages.

## Current Subpackages

- `admin/`: HTML admin dashboard and backend log buffer routes.
- `dcim/`: device, interface, VLAN, and IP sync routes.
- `extras/`: NetBox extras routes used by sync flows.
- `netbox/`: NetBox endpoint CRUD, status, and OpenAPI routes.
- `proxbox/`: Proxbox plugin configuration routes.
- `proxbox/clusters/`: reserved namespace for cluster-specific Proxbox routes.
- `proxmox/`: Proxmox session, node, cluster, replication, viewer, and codegen routes.
- `virtualization/`: virtualization bootstrap and VM sync routes.
- `sync/`: internal sync route helpers used by other route packages, including `sync/individual/`.

## How It Fits Together

- `proxbox_api.app.factory.create_app()` imports routers from these packages and mounts them with prefixes.
- Route modules should expose routers and dependency aliases only; heavy workflow code belongs in `services/`.
- The app factory also mounts root metadata, cache, full-update, and WebSocket routes that live outside this package tree.
- Most route groups depend on schemas from `proxbox_api.schemas` and sync helpers from `proxbox_api.services.sync`.

## Extension Guidance

- Add new route namespaces as subpackages and register them in the app factory.
- Keep request validation and response shaping close to the boundary.
- Convert upstream Proxmox and NetBox errors into `ProxboxException` where the failure is expected.
