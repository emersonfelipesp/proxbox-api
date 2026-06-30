# proxbox_api/routes Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Top-level namespace for FastAPI route packages.

## Current Subpackages and Modules

- `admin/`: HTML admin dashboard and backend log buffer routes.
- `cloud/`: Cloud runtime routes mounted at `/cloud/*`. Includes QEMU templates, image factory, PVE template listing, provision REST and SSE stream, Firecracker provision REST and SSE stream (`cloud/firecracker.py`), catalog, versions, and the **Cloud Image Build Pipeline** (`POST /cloud/templates/images`, `cloud/template_images.py` + `cloud/pipeline_scripts.py`) that bakes Proxmox VM templates with a `cicustom` cloud-init snippet. See `cloud/CLAUDE.md` for the pipeline details and the netbox-packer caller.
- `dcim/`: device, interface, VLAN, and IP sync routes.
- `extras/`: NetBox extras routes used by sync flows.
- `intent/`: Intent plan/apply executor and deletion-request CRUD mounted at `/intent/*`. Includes `vm_tags.py` for best-effort Proxmox tag helpers (tag/untag pending-deletion).
- `netbox/`: NetBox endpoint CRUD, status, and OpenAPI routes.
- `proxbox/`: Proxbox plugin configuration routes.
- `proxbox/clusters/`: reserved namespace for cluster-specific Proxbox routes.
- `proxmox/`: Proxmox session, node, cluster, HA, firewall, SDN, datacenter, access, replication, viewer, and codegen routes. See `proxmox/CLAUDE.md` for the full file inventory.
- `proxmox_actions.py`: operational VM verbs mounted at `/proxmox`, including start, stop, snapshot, migrate, reboot, delete, backup, and snapshot-delete for both QEMU and LXC guests. These handlers enforce `ProxmoxEndpoint.allow_writes` before NetBox/Proxmox side effects, support idempotency keys, and write journal/audit entries per invocation.
  - `POST /proxmox/{vm_type}/{vmid}/reboot?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
  - `DELETE /proxmox/{vm_type}/{vmid}?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
  - `POST /proxmox/{vm_type}/{vmid}/backup?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
  - `DELETE /proxmox/{vm_type}/{vmid}/snapshot/{snapname}?endpoint_id={id}` where `vm_type` is `qemu` or `lxc`
- `ssh_terminal.py`: Browser SSH terminal routes mounted at `/ssh/*`. `POST /ssh/sessions` creates a one-time ticket; WebSocket `/ssh/sessions/{session_id}/ws` bridges a PTY to the target host via `asyncssh`. `GET /ssh/host-key-fingerprint?host=&port=` opens an `asyncssh` handshake (no auth — public host key only) and returns the canonical `SHA256:<base64>` fingerprint so the NetBox plugin can auto-fill the pinned `ssh_known_host_fingerprint`. The scan reuses the exact terminal host-key args (`known_hosts=b""`, `server_host_key_algs="default"`) so the returned value matches what `validate_host_public_key` later verifies. The key is captured two ways for runtime robustness: the `validate_host_public_key` callback, **and** `conn.get_server_host_key()` (read from the connection stashed in `connection_made`) — the deployed asyncssh build treats `known_hosts=b""` as no-verification and skips the callback, so the connection fallback is what makes it work in production.
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
