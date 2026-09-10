# proxbox_api/routes/proxmox Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/proxmox/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Endpoints that expose Proxmox sessions, cluster data, node data, viewer generation, and generated live routes.

## Current Files

- `__init__.py`: Proxmox route handlers for sessions, storage, top-level resource access, and typed VM config helpers.
- `access.py`: Proxmox API token info (GET) and token regeneration (PUT) endpoints (PVE 9.2+).
- `cluster.py`: Proxmox cluster endpoints and cluster response schemas.
- `console.py`: Authenticated service-to-service console-session creation. It obtains the Proxmox VNC/terminal ticket and returns the upstream `wss://` URL, TLS policy, and exactly one private WebSocket authentication value (`authorization` for API-token endpoints or `cookie` for password sessions). The consumer must keep this material in a server-side one-use relay ticket; never expose it to browser JavaScript or logs. The complete code path and invariants are documented in `../../../docs/api/console-sessions.md`.
- `datacenter.py`: Custom CPU models CRUD and datacenter options endpoints (PVE 9.2+).
- `endpoints.py`: Proxmox endpoint CRUD handlers. The create/update/public
  schemas carry `access_methods` (`api` default / `api_ssh`) and the default-off
  `allow_packer_template_builds` capability; a field validator rejects SSH-only
  and unknown access-method values with 422. The narrow packer flag authorizes
  no write by itself and remains subordinate to `allow_writes`.
- `access_gate.py`: `require_ssh_access` / `gate_ssh_access` — the per-endpoint SSH transport gate (`ProxmoxEndpoint.access_methods == 'api_ssh'`), orthogonal to `allow_writes`. Used by the cloud-image / Azure-VHD SSH-execution routes (SQLite-id paths). Returns 403 `reason="ssh_not_enabled_for_endpoint"`.
- `firewall.py`: Datacenter, node, and VM-level firewall endpoints (rules, security groups, IP sets, aliases, options). Read-only by default; write endpoints gated by `ProxmoxEndpoint.allow_writes`.
- `ha.py`: Cluster High-Availability endpoints: status, resources, groups, rules, summary (PVE ≤ 8.x/9.x), plus PVE 9.2+ disarm/arm, manager-status, and CRS config.
- `nodes.py`: Proxmox node endpoints and node interface response schemas.
- `replication.py`: Proxmox cluster replication endpoints.
- `runtime_generated.py`: runtime-generated route registration helpers and cache management.
- `sdn.py`: Software Defined Networking endpoints: fabrics, route-maps, prefix-lists (PVE 9.2+; degrades gracefully on older clusters) plus the read-only `/sdn/create/stream` NetBox reconciliation route with optional `sync_mode_sdn_bgp` projection into `netbox_bgp`.
- `services.py`: read-only agentless service-monitoring route `GET /proxmox/services/systemd?endpoint_id=&units=`. Pulls Proxmox systemd unit state over SSH (fixed-argv `systemctl show -p ...` via `services/proxmox_services.py` + one-shot `run_endpoint_command`) using the endpoint's own registered SSH credential; `endpoint_id` is the netbox-proxbox plugin id (browser-terminal id space), not the SQLite id. Units are regex- + allowlist-validated; an SSH-unreachable endpoint returns 200 `reachable=false`. Called by the RPC executor's `os.linux_proxmox.show_systemctl_services` RPC handler.
- `metrics.py`: authenticated `POST /proxmox/metrics/influx/query` and `POST /proxmox/metrics/pull/query` routes. The first delegates a typed InfluxDB v2 request to `services/influx.py`; the second resolves one configured endpoint and delegates the fixed `cluster/metrics/export` operation to `services/proxmox_metrics.py`. Both return bounded normalized rows and secret-safe errors.
- `viewer_codegen.py`: runtime endpoints to generate and return Proxmox OpenAPI, Pydantic, and live-route artifacts.
- `zfs.py`: read-only tiered ZFS storage inventory routes `GET /proxmox/storage/zfs/pools` and `GET /proxmox/storage/zfs/pools/{pool_name}`. Tier 1 uses the structured Proxmox REST API (`/nodes/{node}/disks/zfs*`) via `proxmox-sdk`; InfluxDB and JSON-native SSH are exposed as ordered fallback seams that currently skip/degrade rather than opening external transports.

## How These Routes Work

- The package uses `ProxmoxSessionsDep` from `session/proxmox.py` for authenticated access.
- Route modules expose typed response schemas and dependency aliases for client-facing API calls.
- Viewer codegen endpoints delegate generation to `proxbox_api.proxmox_codegen`.
- Runtime-generated routes are mounted during application lifespan and also cached to disk so they can be restored on reload.
- Generated routes are served under `/proxmox/api2/{version_tag}` with `/proxmox/api2/*` kept as the `latest` alias.
- Generated dispatch is read-only. `_require_generated_read` rejects every non-GET method before target or credential resolution; the handler calls only `resource.get`. Mutation schemas remain discoverable but deprecated with a documented 403. Preserve this boundary across cached registrations and rebuilds. Do not add generic write forwarding, credential fallback, or a lease-header exception; mutations require dedicated typed, audited procedure handlers. This guard alone does not certify every upstream GET as effect-free or govern handcrafted routes.

## Multi-endpoint dedup (issue #563)

`cluster.py::cluster_resources` deduplicates resources **per cluster identity**
(`px.name`), never globally. Multiple sessions that are nodes of the *same*
cluster each return the full resource list, so same-cluster duplicates are
collapsed; but two *separate* clusters can legitimately share a VMID
(`qemu/100`), so a single global `seen` set would silently drop the second
cluster's resource. Keep the dedup set keyed by cluster identity.

`cluster_status` / `get_node` honor the `proxmox_sessions` selector
(`proxmox_endpoint_ids` / `name` / `domain` / `ip_address`), so the
netbox-proxbox plugin can scope a read to one endpoint and receive only that
endpoint's record(s).

## Extension Guidance

- Keep API wrappers resilient to upstream Proxmox errors and convert them to `ProxboxException`.
- Prefer schema-backed responses for stable client behavior.
- Preserve native scalar types from Proxmox config responses. In particular,
  `allow-ksm` is boolean; route response models must not narrow it to a string.
- Keep runtime route registration and code generation responsibilities separated.
