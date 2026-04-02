# proxbox_api/routes Directory Guide

## Purpose

Top-level route namespace package for FastAPI router modules.

## Current Subpackages

- `admin/`: HTML admin dashboard for NetBox endpoint records.
- `dcim/`: Device, interface, VLAN, and IP sync routes.
- `extras/`: NetBox extras and custom field routes.
- `netbox/`: NetBox endpoint CRUD and plugin configuration routes.
- `proxbox/`: Proxbox plugin configuration routes.
- `proxbox/clusters/`: Cluster-specific Proxbox routes.
- `proxmox/`: Proxmox session, storage, node, and generated viewer routes.
- `virtualization/`: VM and cluster bootstrap routes.

## Key Data Flow and Dependencies

- `proxbox_api.app.factory.create_app()` imports routers from nested route packages and mounts them with prefixes.

## Extension Guidance

- Create new endpoint groups as subpackages and register them in `proxbox_api.app.factory.create_app()`.
