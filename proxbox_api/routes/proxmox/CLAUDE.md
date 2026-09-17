# proxbox_api/routes/proxmox Directory Guide

## Workspace Context

This file lives at `<repository-root>/proxbox_api/routes/proxmox/CLAUDE.md` inside the `personal-context` workspace.
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
- `console.py`: Preserves authenticated service-to-service session creation at
  `POST /console/sessions` and adds the distinct standalone browser contract at
  `POST /console/browser-sessions` plus WebSocket
  `/console/browser-stream`. The private route still returns the
  Proxmox ticket, `wss://` URL, TLS policy, and exactly one authentication
  value. The browser route stores that material only as Fernet ciphertext in
  shared SQLite and returns an opaque 30-second one-use token. Consumption is
  atomic across workers, requires the exact HTTPS Origin and exactly one
  `proxbox-token.<stream_token>` offer alongside `binary`, accepts only
  `binary`, and forbids URI/query tokens. It requires the current endpoint row to remain enabled at create
  and consume, preserves stored `verify_ssl`, and mediates RFB 3.8 VNC
  authentication for noVNC so the ticket never reaches the browser. The
  complete code path and invariants are documented in
  `../../../docs/api/console-sessions.md`.
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
- `viewer_codegen.py`: endpoints to return Proxmox OpenAPI and Pydantic
  artifacts, plus separately exported development-only generation and route
  refresh endpoints. Its aggregate `router` is bundled-only; callers must check
  `runtime_codegen_enabled()` before importing and mounting
  `runtime_codegen_router` explicitly.
- `zfs.py`: read-only tiered ZFS storage inventory routes `GET /proxmox/storage/zfs/pools` and `GET /proxmox/storage/zfs/pools/{pool_name}`. Tier 1 uses the structured Proxmox REST API (`/nodes/{node}/disks/zfs*`) via `proxmox-sdk`; InfluxDB and JSON-native SSH are exposed as ordered fallback seams that currently skip/degrade rather than opening external transports.

## How These Routes Work

- The package uses `ProxmoxSessionsDep` from `session/proxmox.py` for authenticated access.
- Route modules expose typed response schemas and dependency aliases for client-facing API calls.
- Runtime code generation is disabled by default. The application factory mounts
  the generation and refresh endpoints only when the development process starts
  with `PROXBOX_RUNTIME_CODEGEN_ENABLED=true`. Default discovery, route
  registration, and Pydantic rendering read bundled schemas only.
- Runtime-generated routes are mounted during application lifespan. The derived
  route cache may be written to the user-generated directory in either mode,
  but it is eligible for reload only under the development opt-in.
- Runtime-generated Pydantic models are constructed directly from parsed
  OpenAPI data. Startup and route refresh never evaluate the rendered model
  source artifact.
- `GET /proxmox/viewer/pydantic` renders source off the event loop, caches by
  verified schema digest, enforces a 2 MiB result limit and a six-per-minute
  per-source rate limit, and never returns a persisted Python file.
- Bundled version tags are immutable and resolve before user artifacts. User
  schemas require a matching `provenance.json` digest, while artifacts from a
  non-default source URL live under `custom/` and are inspection-only.
- Provenance sidecars detect corruption; they are not authentication, because a
  same-UID writer can forge the artifact and digest. Production therefore keeps
  runtime code generation disabled. Refresh bundled tags only through package
  replacement.
- Startup quarantines user-generated Python model files and runtime route caches
  without valid provenance. The same migration is available through
  `proxbox-schema quarantine-legacy`.
- Fixed document limits are enforced before runtime model construction and
  cache restoration. An invalid cache is ignored without replacing an already
  mounted last-known-good route set.
- Immutable bundled documents are parsed and fully resource-validated once per
  process for each `(resolved path, raw SHA-256)` identity. The bounded cache
  holds at most `MAX_ELIGIBLE_VERSIONS` documents, so replacing a bundled file
  produces a digest miss and a complete new validation. Receipt consumers parse
  the rechecked canonical bytes into private snapshots; aggregate limits, model
  and route construction, cache keys, and route-cache persistence use only
  those snapshots. User-generated and explicitly supplied documents are also
  snapshotted before complete first-sight validation.
- Runtime Pydantic modules use a bounded process-local LRU keyed by
  `(version tag, canonical document SHA-256)`. It holds at most
  `MAX_ELIGIBLE_VERSIONS + 2` modules, rebuilds changed documents, and is never
  shared between worker processes. A two-entry process-local registration-plan
  LRU reuses already constructed `APIRoute` templates when another application
  instance mounts the same validated version/digest set. Each mount receives a
  shallow route clone bound to that application's dependency-override provider.
- The persisted route cache has deterministic compact bytes. Registration
  verifies the existing cache and provenance, compares its SHA-256 with the
  candidate bytes, and rewrites the cache and provenance only when content has
  changed. Existing quarantine and symlink refusal remain in force.
- Model construction cannot be deferred until a version's first request without
  changing the route contract. FastAPI needs the request and response models
  while `add_api_route` constructs dependency fields, response validation, and
  OpenAPI metadata. Process-local module and registration-plan reuse removes
  repeated application-start cost while preserving those eager contracts.
- Aggregate registration is limited to 8 versions, 32 MiB of documents, 16,384
  models, and 32,768 routes before model or cache construction. Route refresh
  persists its complete cache and provenance before swapping mounted routes.
- Generated routes are served under `/proxmox/api2/{version_tag}` with `/proxmox/api2/*` kept as the `latest` alias.
- Viewer generation and refresh accept only contained, validated version tags;
  invalid tags fail with HTTP 422 before crawling or filesystem writes.
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
