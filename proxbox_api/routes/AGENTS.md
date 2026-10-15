# proxbox_api/routes Directory Guide


## Interactive Execution Boundary

Read `docs/operations/interactive-rpc-boundary.md` before changing interactive
admission, SSH or console acquisition, synchronization WebSocket dependencies,
or shutdown ownership. `PROXBOX_EXECUTION_MODE` is process-pinned and defaults
to `rpc_only`; `PROXBOX_EXECUTION_GENERATION` has no generated default. Denial
must occur before eager providers and must also protect capability consumption.
Explicit legacy mode preserves authentication, endpoint restrictions, host-key
pins, and private console TLS/authentication. Keep local readiness scoped and
`aggregate_ready=false`; companion services and fleet coordination are separate
required capabilities. Never add a hot activation endpoint or silently restore
legacy behavior. Preserve native and mounted-ASGI ordering/cancellation tests,
including dependency-upgrade verification, and both language versions of the
documentation.

## Workspace Context

This file lives at `<repository-root>/proxbox_api/routes/CLAUDE.md` inside the `personal-context` workspace.
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
- `ssh_terminal.py`: Browser SSH terminal routes mounted at `/ssh/*`. `POST /ssh/sessions` creates a one-time ticket; WebSocket `/ssh/sessions/{session_id}/ws` bridges a PTY to the target host via `asyncssh`. The create request accepts an optional `one_shot_credential` (`OneShotCredentialInput` → service `OneShotTerminalCredential`) for one-shot/unstored sessions: inline SSH creds held only in the in-memory `TerminalSession` (repr-redacted, never persisted); `fetch_terminal_credential` builds the credential from it and bypasses the stored-credential fetch. Caller-selected hosts on ticket creation and `GET /ssh/host-key-fingerprint?host=&port=` must pass the shared registered-endpoint SSRF resolver before any connection. The service tries only those validated addresses, in order, with bounded TCP timeouts and passes the selected connected socket to AsyncSSH, retaining the original name in `host_key_alias`. The fingerprint endpoint opens a separate unauthenticated handshake and returns the canonical `SHA256:<base64>` fingerprint so the NetBox plugin can pin it. Every credential-bearing terminal or fixed-command connection supplies a truthy empty-known-hosts callback; explicitly disables ambient client configuration, X.509 roots and certificate paths, GSS, agents, host-based authentication, keyboard-interactive authentication, PKCS#11, default keys, and client certificates; and requires `validate_host_public_key` to match the pin during that same connection's key exchange before authentication. TCP reachability failures may advance to the next validated address, but host-key and authentication failures must not. The connection then verifies `conn.get_server_host_key()` again before opening a PTY or running a command. Do not restore a separate pre-authentication scan or any ambient trust source.
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
