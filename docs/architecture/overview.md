# Architecture Overview

`proxbox-api` is organized around FastAPI routing, session dependencies, sync services, and schema layers.

## High-level Layers

- API layer: `proxbox_api/main.py`, `proxbox_api/app/*`, and `proxbox_api/routes/*`
- Session layer: `proxbox_api/session/*`
- Service layer: `proxbox_api/services/*`
- Schema and enum layer: `proxbox_api/schemas/*`, `proxbox_api/enum/*`
- Persistence layer: `proxbox_api/database.py`
- Utility layer: streaming, logging, cache, retry, and error helpers

## Request-Level Caching

NetBox GET requests are cached in-memory to reduce database load during sync operations:

- **Cache location**: `proxbox_api/netbox_rest.py` provides `rest_list_async()`, `rest_first_async()`, etc.
- **TTL**: configurable via `PROXBOX_NETBOX_GET_CACHE_TTL` (default: 60 seconds)
- **Entry limit**: configurable via `PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES` (default: 4096)
- **Byte limit**: configurable via `PROXBOX_NETBOX_GET_CACHE_MAX_BYTES` (default: 52428800 = 50MB)
- **Eviction policy**: LRU (Least Recently Used) when either entry or byte limit is reached
- **Invalidation**: automatic on POST/PATCH/DELETE to related endpoints
- **Observability**: metrics available at `/cache` and `/cache/metrics/prometheus`

Cache invalidation is precise (not prefix-based): updating `/api/dcim/devices/55/` only invalidates that exact path and its parent list, not other device detail paths like `/api/dcim/devices/10/`.

## Runtime Components

- FastAPI app mounts the current route groups:
  - `/`
  - `/cache`
  - `/clear-cache`
  - `/full-update`
  - `/ws`
  - `/ws/virtual-machines`
  - `/admin`
  - `/admin/encryption` — encryption key inspection and rotation surface.
  - `/auth` — bootstrap and API-key management.
  - `/cloud/*` - NMS Cloud VM, LXC, template, image-factory, Azure VHD import, and Firecracker provisioning routes. See [Service Routes](../api/service-routes.md).
  - `/intent/*` - NetBox-to-Proxmox plan/apply/deletion-request safety routes. See [Service Routes](../api/service-routes.md).
  - `/pbs/*`, `/pdm/*`, `/ceph/*`, and `/ceph/v2/*` - optional sidecar service routes. These mount by default when imports succeed, or selectively when `PROXBOX_FEATURES` is set to `pbs`, `pdm`, and/or `ceph`.
  - `/netbox`
  - `/proxmox`
  - `/proxmox/cluster/ha/*` — read-only High-Availability aggregation across configured clusters; see [Cluster HA API](../api/cluster-ha.md).
  - `/proxmox/{qemu,lxc}/{vmid}/{start,stop,snapshot,migrate}` — operational write verbs (plus DELETE-to-cancel and GET-stream for migrate). Gated by `ProxmoxEndpoint.allow_writes`. See [HTTP API Reference — VM Operational Verbs](../api/http-reference.md#vm-operational-verbs).
  - `/dcim`
  - `/virtualization`
  - `/extras`
  - `/sync/individual`
  - `/sync/active` — process-local probe for an in-flight `/full-update` run.
- Sidecar-only mode: when `PROXBOX_FEATURES` contains only optional sidecar flags (`pbs`, `ceph`, `pdm`), the core Proxmox/NetBox/sync/cloud/intent route groups are skipped and only the selected service routes mount alongside root metadata and auth.
- SQLite-backed endpoint configuration and bootstrap state.
- NetBox API access via `netbox-sdk` sync and async clients.
- Proxmox API access via `proxmox-sdk` sync SDK sessions and typed helper wrappers.
- Firecracker host-agent access via `proxbox_api.firecracker_agent.client.FirecrackerHostAgentClient`.
- Runtime-generated Proxmox live routes mounted during app lifespan startup.

## Cloud Image Preflight Trust Boundary and Traceability

The Packer preflight is separated from execution by construction. The route
resolves exactly one enabled persisted endpoint/session, while
`services/packer_preflight.py` receives that session and calls only Proxmox GET
methods. `allow_writes` is reported but cannot block the read. No database
write, Proxmox mutation, SSH command, or task wait is available in the service.
Session creation and upstream check failures become fixed typed diagnostics;
raw exceptions, endpoint schemas, credentials, and process output do not cross
the HTTP or log boundary.

Read-only does not mean advisory. A successful preflight with a
server-rendered `recipe_digest` returns a signed five-minute plan. The recipe
binding is a domain-separated HMAC rather than a dictionary-testable script
hash, and endpoint configuration uses a separate keyed binding. The plan also
binds node, provider, storage roles, and VMID. The preflight still performs no
database mutation. Execution authenticates that plan, reruns the exact GET-only
checks, then authoritatively refreshes and revalidates endpoint/API/SSH authority
again immediately before consuming its UUID and acquiring the unique active
`endpoint_id:vmid` blocker.

Execution has a separate persisted authority boundary. The selected local
`ProxmoxEndpoint` must be enabled and contain one complete node/SSH binding.
The route derives host, user, port, identity path, and pinned host-key
fingerprint from that row; legacy request SSH fields are assertions and cannot
retarget execution. A scanned server key must match the persisted fingerprint
before the exact key is passed to strict OpenSSH host verification. Absolute
SSH binaries, `-F none`, and disabled proxy/canonicalization options isolate the
connection from ambient OpenSSH configuration. The private key is opened once
with `O_NOFOLLOW`, descriptor-verified with `fstat` as a root/service-owned
regular file without group/world permissions, and inherited through
`/proc/self/fd`, so a later symlink or atomic pathname swap cannot replace it.

The lease and state transitions are persisted in
`CloudImageBuildOperation`; scripts, URLs, credentials, cloud-init, and raw
output are intentionally not. SSH runs asynchronously in a unique fixed
`systemd-run` unit while stdout/stderr are drained into counters. Timeout,
request cancellation, and the authenticated operation-cancel route stop that
unit. Mandatory process/unit cleanup, journal updates, and session close finish
through repeated cancellation. Cancel/completion transitions use
compare-and-swap ordering. A zero exit code is only `verification_pending`:
completion requires a final Proxmox API read of the expected artifact. Unknown,
cancelled, recovery, or expired state keeps the unique target blocker as
`recovery_required`, never deleted or released automatically; explicit
reconciliation is intentionally outside this scope.

`CloudImageBuildTarget` is the canonical provider/storage plan for both
preflight and rendering. It derives snippet requirements from the provider;
all providers stage privately in randomized `/var/tmp` directories, while ISO and snippet
destinations resolve from exact volume IDs through `pvesm path`, and unsupported
custom path mappings fail at validation. A neutral
`schemas/cloud_image_security.py` module owns SSH normalization so schema and
route imports have no circular dependency. Request validation is handled by a
fixed response boundary that never serializes Pydantic input or context.

The scoped change is traced to local implementation and verification evidence
below. This organizes candidate cross-chapter traceability (SWE-052),
architecture/design evidence (SWE-057/SWE-058), code and unit-test evidence
(SWE-060/SWE-062), requirements verification evidence (SWE-066), and
test-result evaluation/status evidence (SWE-068). It does not by itself
establish NPR 7150.2D compliance, approval, or independent verification.

| Requirement | Implementation evidence | Verification evidence |
|---|---|---|
| `PF-01` exact endpoint, one enabled session, no first-session fallback | `routes/cloud/template_images.py::_resolve_preflight_target` | multi-session, missing, disabled, ambiguous, and unavailable-session canaries in `tests/test_packer_preflight.py` |
| `PF-02` read-only readiness on write-disabled endpoints | `services/packer_preflight.py::run_packer_preflight` | fake client rejects non-GET methods; `allow_writes=false` and release private-staging tests |
| `PF-03` stable node/storage/VMID/unsupported findings | preflight v1 schemas, provider-derived storage requirements, and authoritative `cluster/nextid?vmid=` | typed finding-shape, ISO/release storage roles, hidden VMID, malformed payload, collision, unsupported-check, and fixture tests |
| `PF-04` secret-safe build/error responses | build response v2, encoded fixed writes, typed source recipes, generic validation handler, execution summary, fixed diagnostics, preview validator | delimiter/source-command/422 and signed-URL/cloud-init/stdout/stderr/session/SDK/cleanup canaries plus preview/execute rejection tests |
| `PF-05` producer/OpenAPI stability | explicit contract versions, canonical `vm_storage`, legacy `storage` alias, and `0.0.21.x` support window | OpenAPI assertions, alias/conflict tests, producer fixture validation, and the explicitly producer-owned consumer-shaped fixture in `tests/fixtures/` |
| `PF-06` persisted execution authority and exact cleanup | refreshed endpoint/node SSH binding, pinned host key, descriptor-pinned private key, ambient-config isolation, and cancellation-safe session cleanup | concurrent endpoint edit, symlink/mode/owner/path-swap, exact SSH argv, fingerprint, repeated cancellation, and exact-close tests |
| `PF-07` approved-plan binding and one owner | domain-separated endpoint/recipe HMACs, signed expiring claims, and retained `CloudImageBuildOperation.lease_key` blockers | credential/recipe oracle, tamper, drift, expiry, replay, concurrent lease, and fail-closed recovery tests |
| `PF-08` verified durable execution | async stream drain, fixed systemd unit, repeated-cancellation cleanup, CAS journal transitions, and final API verification | bounded counter, double/triple cancellation, cancel/completion races, verified completion, forced recovery, and no-success-without-artifact tests |

Image storage requires `iso` only for `proxmox_iso`; release/source providers
use private host staging and make no image-storage claim. VM storage always
requires `images`, and snippets are checked only when the provider-derived
normalized plan needs them. The literal
`import` appears only as the request enum on the mutating storage
`download-url` operation and is intentionally absent from preflight readiness.

Scoped Chapter 4 lifecycle status (this is not a claim that every NPR
7150.2D requirement applies or is complete):

| Phase / requirement | Status | Current evidence | Pending or gap |
|---|---|---|---|
| Requirements — SWE-053, SWE-055 | Partial | tracked feature requirements and `PF-01`–`PF-06` mapping above | consumer validation with the dependent Packer project is pending |
| Architecture — SWE-057 | Partial / local evidence | documented read/write and persisted SSH authority boundaries | independent architecture review and approved deployment validation are pending |
| Design — SWE-058 | Partial / local evidence | versioned Pydantic contracts, normalized target, exact resolver, pure read service, and explicit preview design | downstream consumer conformance and independent review are pending |
| Implementation — SWE-060, SWE-061, SWE-062 | Partial / local evidence | issue-branch code, Ruff/format/compile gates, and focused tests | independent adversarial review and remote CI are pending |
| Testing — SWE-065, SWE-066, SWE-068, SWE-071 | Partial / local evidence | real ASGI auth/success/disabled/malformed/cleanup tests, session cancellation tests, JSON fixture, and focused results | full repository suite, downstream suite, and Gitea CI evidence are pending |
| Model/simulation qualification — SWE-070 | N/A | no flight software or qualification model is used | not applicable |
| Target-platform validation — SWE-073 | Gap | no live Proxmox/NetBox mutation was authorized or performed | package/container and approved staging validation remain pending |
| Operations/delivery — SWE-075, SWE-077 | Partial | API/architecture/agent documentation and compatibility window | release notes, published artifact, deployment, and post-release evidence are pending |

The consumer-shaped fixture is intentionally not counted as downstream
verification. It is maintained by proxbox-api and only checks the producer's
current compatibility intent with an independently declared test model. The
external netbox-packer parser/fixture and downstream suite remain an integration
HOLD. Therefore rollout is fail closed: staging and production must keep
`PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION` unset/false until that consumer-owned
evidence exists. Read-only planning and preflight are unaffected.

## Core Data Models

### NetBox sync-state sidecars

The NetBox plugin owns typed Proxbox sync-state sidecars under
`/api/plugins/proxbox/sync-state/*`. These typed sidecars are now the
**standard** source of truth for the Proxmox-to-NetBox linkage: `proxbox-api`
writes and reads them during sync. The legacy reflection custom fields are
**deprecated** and gated behind the `custom_fields_enabled` plugin setting,
which defaults to `false` — so by default no custom fields are written, read, or
reconciled, and the sidecars stand alone. `proxbox-api` writes these rows during
sync:

- `ProxboxVirtualMachineSyncState` extends `virtualization.VirtualMachine`.
- `ProxboxDeviceSyncState` extends `dcim.Device`.
- `ProxboxClusterSyncState` extends `virtualization.Cluster`.
- `ProxboxVirtualDiskSyncState` extends `virtualization.VirtualDisk`.
- `ProxboxVMInterfaceSyncState` extends `virtualization.VMInterface`.

The sidecars carry the same synchronized data that historically lived only in
custom fields, including VM Proxmox identity, device/cluster timestamps,
VM-interface bridge links, virtual-disk storage links, and VM last-run ids.
Writes use the existing NetBox session and degrade gracefully when an older
plugin does not expose the sidecar API.

**Scope note.** `proxbox-api` populates typed sidecars for the five core object
types listed above (VM, device, cluster, virtual disk, VM interface), which hold
all Proxmox identity and linkage data. The supporting objects synced during a run
(cluster types, manufacturers, device types, device roles, sites) only ever
carried a `proxmox_last_updated` reflection timestamp in custom fields; with
`custom_fields_enabled=false` (the default) that stamp is no longer written, and
the plugin's typed sidecar models for those supporting objects are not populated
by the backend today. This is intentional — supporting objects carry no
Proxmox-to-NetBox linkage — and dropping the stamp has no effect on sync
identity, orphan detection, or reconciliation. Enable `custom_fields_enabled` if
you still need the legacy supporting-object timestamp during a transition.

`proxbox-api` reads the sidecars for custom-field-dependent state. VM identity
and orphan-sweep last-run checks use the typed sidecar rows. With
`custom_fields_enabled=false` (the default) there is **no** legacy `cf_*`
fallback — reads are sidecar-only, and because the sidecars are rebuilt from
live Proxmox data on each sync, a normal re-sync re-adopts existing NetBox VMs
even when the custom fields are already gone. Setting
`custom_fields_enabled=true` restores the legacy behavior for a transition
period (dual-writing custom fields and using the `cf_*` read fallback), and
every custom-field code path then emits a deprecation warning. Role-ownership
snapshots have no sidecar field and are only read when the flag is enabled. Full
custom-field retirement remains a later migration item; no custom-field data is
deleted while the flag exists.

### `NetBoxEndpoint`

- Fields: `name`, `ip_address`, `domain`, `port`, `token_version`, `token_key`, `token`, `verify_ssl`
- Supports both NetBox token v1 and v2 shapes.
- Includes computed `url` property for NetBox session creation.
- API-level singleton behavior is enforced by create endpoint logic.

### `ProxmoxEndpoint`

- Core/API fields: `name`, `ip_address`, `domain`, `port`, `username`,
  `password`, `verify_ssl`, `token_name`, `token_value`, `enabled`,
  `allow_writes`, and `access_methods`.
- Optional Cloud Image execution binding: `ssh_target_node`, `ssh_host`,
  `ssh_username`, `ssh_port`, `ssh_identity_file`, and
  `ssh_known_host_fingerprint`; executable builds require the complete set.
- `domain` is optional and `name` is unique.
- Supports either password auth or token-pair auth.

## Startup Flow

1. `create_app()` initializes the database and NetBox bootstrap state.
2. The app mounts static assets, CORS middleware, exception handlers, cache routes, full-update routes, and WebSocket routes.
3. Route packages are included for NetBox, Proxmox, DCIM, virtualization, extras, and individual sync helpers.
4. Generated Proxmox live routes are mounted during lifespan startup and can fail open unless `PROXBOX_STRICT_STARTUP` is enabled.
5. The custom OpenAPI builder embeds the generated Proxmox OpenAPI contract when one is available.

## OpenAPI Extension

`proxbox_api/openapi_custom.py` overrides FastAPI OpenAPI generation and embeds generated Proxmox OpenAPI metadata when available:

- Source file: `proxbox_api/generated/proxmox/latest/openapi.json`
- Extension fields:
  - `info.x-proxmox-generated-openapi`
  - `x-proxmox-generated-openapi`

## Sync Lifecycle

- Sync endpoints orchestrate Proxmox discovery and NetBox object creation.
- Journal entries and sync-process records are used for traceability.
- WebSocket and SSE streaming endpoints provide real-time sync progress with per-object granularity.
