# proxbox-api Project Guide


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

This file lives at `<repository-root>/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

> **LLM Agent Safety — Destructive Operations:** proxbox-api exposes routes
> that permanently destroy Proxmox VMs, LXC containers, snapshots, and backups.
> **Never invoke `DELETE /proxmox/{vm_type}/{vmid}`, snapshot-delete, or backup-delete
> autonomously.** Every write verb requires `ProxmoxEndpoint.allow_writes=True`
> (default: `False`) and an `X-Proxbox-Actor` header. Stop/reboot require user
> notification before invocation. See `AGENTS.md` §"LLM Agent Safety Guardrails"
> for the full protocol. Enforcement anchors: `proxbox_api/database.py::ProxmoxEndpoint.allow_writes`,
> `proxbox_api/routes/proxmox_actions.py::_gate`, and `tests/test_static_guardrails.py`.

---

## Overview

The explicit developer-only mounted operation inventory is documented in
`docs/operations/operation-inventory.md`; its nearest implementation guide is
`proxbox_api/operation_inventory/CLAUDE.md`. Preserve all twenty-two feature
inputs and fifteen reachable states, ordered duplicates, generated identities,
and exact locked source evidence. Generation, drift verification, and unresolved
coverage readiness are separate commands. The inventory does not authorize
execution, classify effects from methods, or activate the RPC-only cutover.
Generated contract tables stay under `contracts/` and are embedded into the
handwritten bilingual documentation by restricted build-time snippets.

`proxbox-api` is a FastAPI backend that connects Proxmox inventory and lifecycle data to NetBox objects. It serves REST, SSE, and WebSocket endpoints for discovery, synchronization, endpoint management, generated Proxmox proxy routes, and Firecracker host-agent provisioning for the cloud management runtime. The same repository also includes a standalone `nextjs-ui/` frontend for endpoint administration.

### Companion repos (cross-link map)

Generated `/proxmox/api2/*` proxy dispatch is read-only. Every non-GET method is refused before target and credential resolution, including cached and rebuilt routes. Mutation schemas remain discoverable but deprecated with a documented 403; use dedicated typed, audited RPC procedures for supported writes. This method guard does not establish effect safety for all GET operations or change handwritten route authorization.

## Proxmox Code Generation Security

Runtime code generation is disabled by default. `PROXBOX_RUNTIME_CODEGEN_ENABLED=true`
is a development-only, process-level opt-in. Without it, the application factory
does not mount `POST /proxmox/viewer/generate` or
`POST /proxmox/viewer/routes/refresh`; `GET /proxmox/viewer/openapi` and
`GET /proxmox/viewer/pydantic` accept bundled tags only; and runtime discovery
never scans the user-generated directory for schemas. The directory may still
hold the derived runtime route cache. A bundled tag, including `latest`, can be
refreshed only by replacing the installed package with one containing the new
schema.

Runtime-generated routes construct Pydantic models directly from parsed OpenAPI
data with `pydantic.create_model`; runtime startup and route refresh never
evaluate rendered Python source. The file renderer remains available for
offline artifacts, but it must validate every emitted identifier and render
aliases, descriptions, and defaults with Python literal representations.
Codegen version tags must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` and must
not equal `.` or `..`. Validate tags before any crawl or write, and resolve
every generated artifact and runtime route-cache path inside its configured
base directory before accessing the filesystem.
Bundled schema tags are immutable and always take precedence over user-generated
artifacts. A user-generated schema is eligible for route registration only when
its `provenance.json` sidecar identifies the source and generation time and its
SHA-256 digest matches `openapi.json`. Schemas generated from a non-default
source URL are stored under the `custom/` namespace for offline inspection and
must never be registered as runtime routes. Enforce the OpenAPI byte, depth,
path, operation, property, model, and string limits before persistence, cache
use, or Pydantic model construction. Application startup quarantines persisted
Python model files and legacy runtime route caches without valid provenance;
operators can run the same upgrade step with `proxbox-schema quarantine-legacy`.
Provenance sidecars detect corruption; they do not authenticate an artifact,
because another process running as the same operating-system user can forge the
artifact and its digest. Production therefore keeps runtime code generation off.

- **`netbox-proxbox` 0.0.27rc4 (current source)** — the NetBox plugin that
  consumes this backend. Source:
  <https://github.com/emersonfelipesp/netbox-proxbox>. The current sibling-source
  development pairing is `netbox-proxbox 0.0.27rc4 ... proxbox-api 0.0.23 ... proxmox-sdk 0.0.15 ... netbox-sdk 0.0.13`.
  This source-only tuple is not a published or certified runtime pairing.
  `proxbox-api 0.0.19` ships Proxmox SDN sync collectors, NetBox L2VPN,
  RouteTarget, Prefix reconcile, plugin inventory reconciliation, and
  VM-interface reconcile idempotency hardening. Operational-verb routes (start/stop/snapshot/migrate)
  require `proxbox-api >= 0.0.17`; firewall model scaffolding and intent tag
  helpers require `>= 0.0.13`; HA tab and runtime tunables alone require `>= 0.0.11`.
  Firecracker Cloud uses the plugin for host pools, host-agent inventory, image
  templates, and `FirecrackerMicroVM` rows while this backend calls the selected
  host-agent through `/cloud/firecracker/*`.
- **Workspace note**:
  `personal-context/claude-reference/proxbox-api.md` (deep-dive index of this
  repo) and `personal-context/claude-reference/netbox-proxbox.md` (deep-dive
  index of the plugin) live in the AI workspace and should be kept in sync
  when route prefixes, env vars, or required dependency floors change.

## Use This Index First

Open the nearest scoped guide for the code you are changing.

### Top-level packages

- `proxbox_api/CLAUDE.md` — Core FastAPI package overview
- `proxbox-reconcile-rs/CLAUDE.md` — Optional Rust VM reconciliation engine
- `proxmox-mock/CLAUDE.md` — Local dev mock Proxmox service (`proxmox-mock-api`; editable dep)
- `nextjs-ui/CLAUDE.md` — Next.js frontend for endpoint management
- `nextjs-ui/AGENTS.md` — Frontend agent quick-reference

### Infrastructure and tooling

- `.github/CLAUDE.md` — CI/CD workflow descriptions
- `docker/CLAUDE.md` — Container runtime and proxy configuration
- `docs/CLAUDE.md` — MkDocs documentation structure
- `tests/CLAUDE.md` — Backend test suite layout and conventions
- `scripts/CLAUDE.md` — Utility and maintenance scripts
- `tasks/CLAUDE.md` — Development task tracking
- `automation/CLAUDE.md` — Automation entry points
- `proxmox-mock/CLAUDE.md` — Mock Proxmox service used in tests

### proxbox_api subpackages

- `proxbox_api/app/CLAUDE.md` — Application factory and lifecycle
- `proxbox_api/routes/CLAUDE.md` — Route package index
- `proxbox_api/routes/admin/CLAUDE.md` — Admin dashboard routes
- `proxbox_api/routes/cloud/CLAUDE.md` — Cloud runtime routes + the Cloud Image Build Pipeline (`/cloud/templates/images`, cicustom cloud-init bake)
- `proxbox_api/routes/dcim/CLAUDE.md` — DCIM device routes
- `proxbox_api/routes/extras/CLAUDE.md` — Extras bootstrap-status route
- `proxbox_api/routes/netbox/CLAUDE.md` — NetBox endpoint CRUD routes
- `proxbox_api/routes/proxbox/CLAUDE.md` — Proxbox plugin routes
- `proxbox_api/routes/proxbox/clusters/CLAUDE.md` — Cluster route namespace
- `proxbox_api/routes/proxmox/CLAUDE.md` — Proxmox proxy and codegen routes
- `proxbox_api/routes/sync/CLAUDE.md` — Internal sync helper routes
- `proxbox_api/routes/virtualization/CLAUDE.md` — Virtualization routes
- `proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md` — VM sync routes
- `proxbox_api/services/CLAUDE.md` — Service layer index
- `proxbox_api/services/sync/CLAUDE.md` — Sync workflow services
- `proxbox_api/services/sync/reconciliation/CLAUDE.md` — VM reconciliation seam and engine modes
- `proxbox_api/services/sync/individual/CLAUDE.md` — Individual object sync services
- `proxbox_api/session/CLAUDE.md` — Session and client factories
- `proxbox_api/schemas/CLAUDE.md` — Pydantic schema index
- `proxbox_api/schemas/netbox/CLAUDE.md` — NetBox domain schemas
- `proxbox_api/schemas/netbox/dcim/CLAUDE.md` — DCIM schemas
- `proxbox_api/schemas/netbox/extras/CLAUDE.md` — Extras schemas
- `proxbox_api/schemas/netbox/virtualization/CLAUDE.md` — Virtualization schemas
- `proxbox_api/schemas/virtualization/CLAUDE.md` — VM-level schemas
- `proxbox_api/enum/CLAUDE.md` — Enum/choice values index
- `proxbox_api/enum/netbox/CLAUDE.md` — NetBox enums
- `proxbox_api/enum/netbox/dcim/CLAUDE.md` — DCIM enums
- `proxbox_api/enum/netbox/virtualization/CLAUDE.md` — Virtualization enums
- `proxbox_api/proxmox_codegen/CLAUDE.md` — Proxmox API crawler and generator
- `proxbox_api/proxmox_to_netbox/CLAUDE.md` — Proxmox-to-NetBox transformation
- `proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md` — Object mappers
- `proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md` — Transformation schemas
- `proxbox_api/generated/CLAUDE.md` — Generated artifacts (do not edit)
- `proxbox_api/generated/netbox/CLAUDE.md` — NetBox model snapshots
- `proxbox_api/generated/proxmox/CLAUDE.md` — Proxmox model snapshots
- `proxbox_api/types/CLAUDE.md` — Type aliases and protocols
- `proxbox_api/utils/CLAUDE.md` — Shared utilities
- `proxbox_api/custom_objects/CLAUDE.md` — Custom NetBox object wrappers
- `proxbox_api/diode/CLAUDE.md` — Diode sandbox integration
- `proxbox_api/e2e/CLAUDE.md` — E2E browser test helpers

## Repo Structure

- `proxbox_api/`: FastAPI package, session factories, schemas, routes, sync services, code generation, and shared utilities.
- `proxbox-reconcile-rs/`: optional PyO3/maturin Rust package for VM operation-queue reconciliation parity testing and opt-in execution.
- `proxmox-mock/`: Standalone `proxmox-mock-api` dev-dependency package (editable install from `pyproject.toml` `[tool.uv.sources]`). Used in tests as the mock Proxmox server. Note: `proxmox-sdk` is an **external pinned package** (`proxmox-sdk==0.0.15` in `pyproject.toml`), not a local subdirectory.
- `nextjs-ui/`: Next.js frontend used to manage one NetBox endpoint and multiple Proxmox endpoints.
- `tests/`: Unit, integration, and end-to-end tests for the backend package.
- `benchmarks/`: local benchmark helpers, including VM reconciliation queue datasets and timers.
- `docs/`: MkDocs documentation, including English and Brazilian Portuguese content.
- `scripts/`: Utility scripts, including schema refresh helpers.
- `automation/`: Placeholder for future automation workflows.
- `tasks/`: Development task tracking.
- `Dockerfile` and `docker/`: runtime and reverse-proxy images for local and published deployments.
- `.github/workflows/`: CI/CD pipelines for test, lint, publish, and docs.

## Architecture

### Core layers

- API and app composition (`proxbox_api/app/*`, `proxbox_api/main.py`, `proxbox_api/routes/*`): create the FastAPI app, register routers, mount middleware, expose WebSocket and SSE streams, and keep request handlers thin.
- Firecracker host-agent layer (`proxbox_api/routes/cloud/firecracker.py`, `proxbox_api/firecracker_agent/`, `proxbox_api/schemas/firecracker.py`): validates Cloud provisioning payloads, including the caller-supplied host-agent URL through the shared SSRF guard, calls host-agent health/capacity/assets/create/action endpoints, and emits the streaming progress contract consumed by the management backend.
- Authentication layer (`proxbox_api/auth.py`, `proxbox_api/routes/auth.py`): bcrypt-hashed API key storage, `X-Proxbox-API-Key` header enforcement via `APIKeyAuthMiddleware`, brute-force lockout, and bootstrap flow for first-time key registration.
- Session and dependency layer (`proxbox_api/session/*`, `proxbox_api/dependencies.py`): create NetBox and Proxmox client sessions from database or plugin configuration.
- Service layer (`proxbox_api/services/*`): implement synchronization workflows, object reconciliation, and reusable helper logic.
- Schema and enum layer (`proxbox_api/schemas/*`, `proxbox_api/enum/*`): validate payloads, normalize data, and define contract-safe choice values.
- Transform and codegen layer (`proxbox_api/proxmox_to_netbox/*`, `proxbox_api/proxmox_codegen/*`, `proxbox_api/generated/*`): turn Proxmox data into NetBox payloads and generate contract artifacts.
- Support layer (`proxbox_api/utils/*`, `proxbox_api/logger.py`, `proxbox_api/cache.py`, `proxbox_api/exception.py`, `proxbox_api/netbox_rest.py`, `proxbox_api/openapi_custom.py`): logging, streaming, caching, and exception helpers.
- Demo and e2e layer (`proxbox_api/e2e/*`): Playwright authentication helpers and shared fixtures for browser-backed tests.

### Runtime flow

1. `proxbox_api.app.factory.create_app()` builds the application object and wires shared
   middleware, routers, and exception handlers. It deliberately resolves **no** database or
   NetBox configuration: importing or constructing the app must never touch the filesystem or
   the network, so configuration errors surface at startup rather than at import time.
2. Each lifespan acquires an opaque ownership token for the process-shared database and
   authentication runtime before it calls `bootstrap.init_database_and_netbox()`. It then
   validates the authentication lockout identity key, registers generated Proxmox proxy routes,
   builds the default NetBox session, and records bootstrap status. One owner initializes and
   atomically publishes the shared bootstrap globals for each runtime generation; concurrent
   owners wait for and reuse that result. Startup failures release only the token they acquired,
   and overlapping lifespans keep the shared engines, runtime lease, and lockout identity alive
   until cancellation-resistant final disposal completes. Repeated cancellation is deferred until
   every cleanup task reaches a terminal state. Engine-disposal failure attempts both engines,
   keeps the runtime lease and identity pinned, and poisons database reuse until process restart;
   cleanup errors never replace an earlier startup or application failure. The shared async
   engine uses `NullPool`, so overlapping lifespans on distinct event loops never reuse a
   loop-bound pooled connection. Blocking lifecycle waiters and the synchronous cleanup work
   that wakes them use separate dedicated executors, so default-executor saturation cannot
   deadlock final disposal.
3. Requests resolve NetBox and Proxmox sessions through dependency providers.
4. VM sync routes prepare Proxmox/NetBox state, then delegate deterministic VM
   operation-queue reconciliation to `proxbox_api.services.sync.reconciliation`.
5. Route handlers delegate remaining heavy work to service modules and schemas.
6. Firecracker Cloud routes under `/cloud/firecracker/*` call a selected host-agent VM after the management backend resolves NetBox Proxbox inventory and creates the `FirecrackerMicroVM` row.
7. Sync and provisioning runs emit journal entries, structured logs, and optional WebSocket or SSE progress messages.

### Route Group Map

For the complete HTTP route reference including schemas and error shapes, see [`docs/api/http-reference.md`](docs/api/http-reference.md).

Key route groups mounted in `proxbox_api/app/factory.py`:

- **Proxmox operational verbs** (`proxbox_api/routes/proxmox_actions.py`, mounted at `/proxmox`): start, stop, snapshot, migrate, reboot, delete, backup, and snapshot-delete for QEMU and LXC guests. All gated by `ProxmoxEndpoint.allow_writes`.
- **Browser console sessions** (`proxbox_api/routes/proxmox/console.py`): keep
  private `POST /proxmox/console/sessions` unchanged for the trusted management
  relay. The separate standalone `POST /proxmox/console/browser-sessions` and
  WebSocket `/proxmox/console/browser-stream` surface returns only an
  opaque 30-second one-use token, expiry, console type, and path. Its full
  upstream payload is Fernet-encrypted in shared SQLite and atomically consumed
  across workers; no encryption means no browser relay. Bind the token to the
  exact validated HTTPS Origin and exactly one `proxbox-token.<stream_token>`
  offered protocol alongside `binary`, while accepting only `binary`; never put
  the token in a URI. Preserve stored
  `verify_ssl`, and mediate RFB 3.8 VNC authentication server-side for QEMU
  noVNC. QEMU/LXC terminal modes relay directly; LXC noVNC remains invalid.
  Refuse all upstream redirects before a second connection so credentials are
  never replayed. Keep client errors, close reasons, and logs secret-free and retain the
  inactive create/consume policy seam for the pending RPC-only endpoint policy.
  Require the current endpoint row to be enabled at both standalone boundaries;
  this browser-only rule must not change the existing service-only broker.
  Preflight Fernet before acquiring the upstream ticket, bracket IPv6
  authorities, validate node path segments and the relay expiry index, and use
  one idle deadline refreshed by either relay direction.
  Read [`docs/api/console-sessions.md`](docs/api/console-sessions.md) before
  changing either contract.
- **Proxmox config tags** (`proxbox_api/routes/proxmox_tags.py`, mounted at `/proxmox`): `PUT/PATCH /proxmox/{qemu|lxc}/{vmid}/tags?endpoint_id=` replace or merge Proxmox guest config tags via `config.put(tags=...)`. Reuses `_gate` from `proxmox_actions` and tag helpers from `routes/intent/vm_tags.py`. Body: replace `{ "node", "tags" }`; merge `{ "node", "add"?, "remove"? }`.
- **High-Availability** (`routes/proxmox/ha.py`, `/proxmox/cluster/ha/*`): status, resources, groups, rules, summary, disarm, arm, manager-status, CRS config.
- **Firewall** (`routes/proxmox/firewall.py`, `/proxmox/firewall/*`): datacenter, node, and VM-level rules, security groups, IP sets, aliases, and options. Write endpoints gated by `allow_writes`.
- **SDN** (`routes/proxmox/sdn.py`, `/proxmox/sdn/*`): controllers, zones, VNets, VNet subnets, fabrics, route-maps, prefix-lists, node runtime rows, read-only `create/stream` NetBox reconciliation, and optional `netbox_bgp` projection when `sync_mode_sdn_bgp` is enabled. Unsupported older clusters and missing optional BGP plugin APIs are skipped with warnings instead of failing the stream.
- **Datacenter** (`routes/proxmox/datacenter.py`, `/proxmox/datacenter/*`): custom CPU models CRUD + datacenter options (PVE 9.2+).
- **Access** (`routes/proxmox/access.py`, `/proxmox/access/*`): token info GET and token regeneration PUT (PVE 9.2+).
- **Service monitoring** (`routes/proxmox/services.py`, `/proxmox/services/*`): `GET /proxmox/services/systemd` reads systemd unit status (`Id`, `LoadState`, `ActiveState`, `SubState`, `Result`, `MainPID`, `ExecMainCode`, `ExecMainStatus`, `NRestarts`, `ActiveEnterTimestamp`, `UnitFileState`) for a Proxmox endpoint over SSH, using the endpoint's own registered SSH credential (agentless — no Proxmox-side agent required). Gated on: NetBox `ProxmoxEndpoint` enabled, `service_monitoring_enabled`, `allow_writes`, `access_methods=api_ssh`, complete SSH credentials, and netbox-rpc not disabled for the endpoint. Bounded 10s SSH command timeout; unit names are validated (`^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$`, no `..`, ≤100 chars, ≤32 units/request) and `shlex.quote`'d as defense in depth before the fixed-argv `systemctl show` command runs. `reachable=False` (SSH unreachable) is returned as HTTP 200 — a legitimate monitoring result — while unknown endpoint id / missing or disabled SSH credential / malformed unit request surface as 4xx. Called by the RPC executor's `@rpc_handler("os.linux_proxmox.show_systemctl_services")` via the matching netbox-rpc procedure, not meant to be called directly by end users. See `routes/proxmox/CLAUDE.md`.
- **Metrics queries** (`routes/proxmox/metrics.py`, `/proxmox/metrics/*`): authenticated bounded routes provide structured InfluxDB v2 queries and direct Proxmox pulls. The Influx route constructs escaped Flux server-side, accepts no arbitrary Flux, and bounds both upstream and normalized output bytes. The pull route resolves one configured endpoint and calls only `cluster/metrics/export`, with no caller-supplied path. Both enforce response and row bounds and map failures to secret-safe reasons. Pull response bytes are bounded during the authenticated upstream stream, boolean parameters use Proxmox-compatible encodings, and redirects are rejected; `services/proxmox_bounded.py` calls the SDK's public `get_bounded()` (proxmox-sdk >= 0.0.15), encodes booleans as `0`/`1` because the SDK forwards query values verbatim, and maps `ResponseTooLargeError`/`UnsupportedResponseEncodingError` to backend-typed errors. See `routes/proxmox/CLAUDE.md`.
- **Cloud** (`routes/cloud/`, `/cloud/*`): live QEMU Cloud-Init template discovery (`GET /cloud/vm/templates`), image factory, PVE templates, catalog, provision (REST + SSE stream), Firecracker provision (REST + SSE stream), versions, the **Cloud Image Build Pipeline** (`POST /cloud/templates/images`): bakes a Proxmox VM template from a base image + a verbatim `user_data_yaml` `#cloud-config` written as a `cicustom` user-data snippet (the only mechanism that runs a full `#cloud-config` at first boot), and the **Azure VHD Import Pipeline** (`POST /cloud/azure/vhd-imports`): preflights the destination node/storage/bridge/VMID, downloads an Azure-exported VHD, validates and converts it to QCOW2, creates the VM shell, imports the disk, and attaches the imported volid parsed from `qm importdisk` output with Linux or Windows-safe defaults. PVE catalog builds must use `provider="proxmox_iso"` with official Proxmox VE installer ISO media and must reject `debian_cloud_image`; generated PVE setup uses graphical VGA for noVNC, while `serial0` + `vga serial0` is reserved for intentional serial appliance products such as pfSense and OPNsense. QEMU provisioning accepts optional `sockets`, `bridge`, `vlan_tag`, `disk_gb`, and `enable_agent` (default `True`) overrides plus a `cloud_init.password` (written as Proxmox `cipassword` for username+password SSH) and applies them through the Proxmox API during clone configuration. `enable_agent` forces `agent=enabled=1` on the clone regardless of the source template. The Cloud Image Build Pipeline SSH execution path also sets `qm ... --agent enabled=1` before templating so clones inherit Proxmox-side QEMU guest agent support. Execution remains gated by `PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION=true`; `execute=true` requires `endpoint_id`, `ProxmoxEndpoint.allow_writes=True`, and `ProxmoxEndpoint.access_methods="api_ssh"` before any SSH script can run. SSH identities stay restricted to `PROXBOX_SSH_KEY_DIR`; the runtime image bakes in `openssh-client`. Called by `netbox-packer` (cloud_config installer) and the management route `/cloud/azure-to-proxbox-migration`. See `routes/cloud/CLAUDE.md`.
- **Intent** (`routes/intent/`, `/intent/*`): plan, apply, deletion-requests, tag/untag pending-deletion.
- **Ceph v2 control plane** (`proxbox_api/ceph/`, `/ceph/v2/*`): Proxmox plans require one explicit durable local endpoint, one request-private full-schema HMAC-bound session, and one exact node per non-noop operation; first-node/`localhost` mutation fallbacks are forbidden. `netbox-ceph` resolves its plugin endpoint to this canonical backend endpoint ID; its plugin PK is not interchangeable. The canonical plan/digest, strict typed payload for each `(kind, action)`, stable server-keyed endpoint revision, hashed approval, owner-bound run lease, append-only dispatch/task events, and permanent provider-global task claims are persisted; a distinct delegated actor issues one opaque, expiring approval and the requester consumes it atomically once. `enabled`, `allow_writes`, revision, endpoint/session binding, and node are rechecked before every mutation, with freshness queries serialized against lease heartbeats. Task-based mutations atomically claim one complete UPID globally for the provider; only SDK-proven flag create/update/delete and OSD update may declare typed synchronous completion. Missing/multiple/reused/node-inconsistent UPIDs, expired leases, crash/cancellation, late workers, and ambiguous legacy cross-endpoint claims are never replayed or promoted to success. Post-dispatch evidence and cancellation checkpoints survive repeated `cancel()` calls until durable completion. Recursive persistence/API/SSE/log redaction covers normalized secret aliases, URLs, extras, exceptions, non-JSON fallbacks, and tracebacks. Writes are default-off unless both `PROXBOX_ENABLE_CEPH_V2_WRITES=true` and `PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY=true`; Dashboard/external apply and destructive capabilities stay false until durable provider authority exists, and the authenticated NetBox gateway must overwrite `X-Proxbox-Actor`. See `proxbox_api/ceph/CLAUDE.md` and `docs/operations/ceph-write-approvals.md`.
- **Ceph timing, authority, and failure isolation**: every Proxmox Ceph mutation prepares through fresh typed `cluster/status` membership and an uncached endpoint/session gate, while an independent audit/lease session keeps heartbeating. The engine performs another live owner/expiry CAS after preparation and before invoking the prepared mutation boundary; renewal/checkpoint predicates evaluate database wall-clock time after row-lock waits so delayed statements cannot reclaim expired authority. `ceph_task_timeout`, `ceph_task_poll_interval`, and `ceph_run_lease_seconds` resolve once off-loop as environment override → plugin setting → default; poll interval is normalized to at most timeout, every task-status call/sleep uses the remaining deadline, and each run persists its immutable lease duration. Ambiguous provider-task migration collisions abort application construction, and sensitive-data filtering covers the DEBUG admin buffer as well as normal handlers.
- **SSH Terminal** (`routes/ssh_terminal.py`, `/ssh/*`): `POST /ssh/sessions` creates a one-time ticket; WebSocket `/ssh/sessions/{session_id}/ws` bridges the PTY. `GET /ssh/host-key-fingerprint?host=&port=` scans a host's SSH key (no auth — public key only) and returns its canonical `SHA256:<base64>` fingerprint for pinned-fingerprint auto-fill in the NetBox plugin; the scan mirrors the terminal connect args so the value matches what the session later verifies. The terminal's `endpoint_id` is the **NetBox-side** `ProxmoxEndpoint` id, not the proxbox-api SQLite id, so the per-endpoint SSH access-method gate (`access_methods=api_ssh`) for the terminal is enforced in the `netbox-proxbox` plugin at the SSH-credential-serving endpoint — this route is intentionally not SQLite-gated. `POST /ssh/sessions` also accepts an **optional `one_shot_credential`** object (`username`, `port`, `known_host_fingerprint`, `password?`, `private_key?`) for **one-shot (unstored) sessions**: the NetBox plugin supplies inline credentials the operator typed into the Terminal modal for a single connection. The material lives only in the in-memory `TerminalSession` for the ticket TTL, is redacted from `repr()`/logs, and is **never persisted** — `fetch_terminal_credential` builds the credential from it and skips the netbox-proxbox stored-credential fetch entirely (the shared `hardware_discovery.fetch_credential` used by background discovery is untouched). A pinned `known_host_fingerprint` remains mandatory (an empty fingerprint canonicalizes to `SHA256:` and never matches). The field is additive/optional; older callers that omit it are unaffected. Requests without inline creds still fetch stored `NodeSSHCredential` / endpoint-fallback credentials as before.
- **Transport access method** (`ProxmoxEndpoint.access_methods`, enum `proxbox_api/enum/proxmox.py::ProxmoxAccessMethod`): per-endpoint axis orthogonal to `allow_writes`. `api` (default, new endpoints) = Read+Write over API only; `api_ssh` = API + SSH. SSH-only is unrepresentable (two-value enum; create/update reject any other value with 422). Existing rows are backfilled to `api_ssh` on upgrade (non-breaking). Gates proxbox-api's own SQLite-id SSH paths (Cloud Image Build Pipeline, Azure VHD import) via `routes/proxmox/access_gate.py`. The value is pushed from the NetBox plugin and accepted on `POST/PUT /proxmox/endpoints`.
- **Extras status** (`routes/extras/`, `/extras/*`): `GET /extras/bootstrap-status` exposes startup bootstrap warnings. The former custom-field creation and reconciliation routes have been removed; typed `Proxbox*SyncState` sidecars are the only Proxbox reflection-state store.
- **Sync** (`routes/sync/`, `/sync/*`): individual and active sync endpoints.
  Proxmox node Device names use the effective endpoint/global
  `node_device_name_template`, while Proxmox API paths and typed sync-state
  identity retain the original short node name. The supported placeholders are
  `{node}`, `{cluster}`, `{cluster_slug}`, and `{endpoint}`.
- **Optional sidecars** (conditionally mounted): `/pbs/*`, `/ceph/*`, `/pdm/*` when the corresponding `proxmox-sdk` extras are installed and `PROXBOX_FEATURES` includes them.

## Error and data rules

- Use `ProxboxException` for expected API failures.
- NetBox transport failures are mapped in `netbox_rest._handle_netbox_error()`:
  timeouts (`TimeoutError`, all aiohttp timeout classes) become HTTP 504 and
  connection failures (`aiohttp.ClientConnectionError`) HTTP 502, both with a
  non-empty `detail` built by `utils.retry.describe_exception()` (class name +
  text, because `str()` of a timeout is empty). `utils.retry` classifies them
  as transient by type, so the retry loop and the plugin's 5xx stage retry
  both engage. Never emit an empty `detail`; `dependencies.proxbox_tag()`
  falls back through `python_exception` and the cause.
- Keep parsing and normalization inside Pydantic schemas, especially in `proxbox_api/proxmox_to_netbox/`.
- Keep generated artifacts under `proxbox_api/generated/` out of manual editing unless you are debugging generation itself. These artifacts serve the backend proxy/viewer surface; the exactly pinned `proxmox-sdk` generated models are the single runtime response-validation authority used by sync helpers.
- Preserve parity between WebSocket progress payloads and SSE payloads.
- Prefer `proxbox_api.logger.logger` over `print`.

## Entry Points

- ASGI app: `proxbox_api.main:app`
- Typical server command: `uvicorn proxbox_api.main:app --host 127.0.0.1 --port 8000`
- Docker entrypoint: the `Dockerfile` uses the same app module path.
- CLI: `proxbox-proxmox-codegen` (`proxbox_api.proxmox_codegen.cli:main`) — Proxmox crawler/generator pipeline.
- CLI: `proxbox-schema` (`proxbox_api.schema_cli:main`) — list, status, and generate NetBox-versioned schema artifacts.
- Smoke tests live under `tests/` (for example `tests/test_main_smoke.py` and `tests/test_endpoint_crud.py`)

## Dependencies

- Runtime: `fastapi[standard]`, `proxmox-sdk==0.0.15` (external PyPI package), `netbox-sdk==0.0.13` (external PyPI package), `sqlmodel`, `aiosqlite`, `cryptography`, `bcrypt`, `asyncssh>=2.20.0,<3.0.0`. The service continues to pass `NETBOX_SCHEMA_VERSION = "4.6"` explicitly because its certified deployment matrix ends at NetBox 4.6.6; the SDK's 4.7 fallback default is therefore not selected.
- Tests: `pytest`, `httpx`, `playwright`, `pytest-cov`, `pytest-asyncio`, `pytest-xdist`
- Docs: `mkdocs`, `mkdocs-material`, `mkdocs-static-i18n`

## Environment Variables

Most runtime tunables now resolve in order **env var > `ProxboxPluginSettings` (NetBox plugin settings page) > built-in default**, via `proxbox_api/runtime_settings.py`. Setting an env var still works as an override; leaving it unset means the plugin settings page is the authoritative source. The settings cache TTL is 5 minutes, so plugin-side changes take effect without a restart.

### Adding a new tunable

**Configuration policy — prefer DB-backed plugin settings.**
When adding a new runtime tunable, default to making it a `ProxboxPluginSettings` field
(NetBox-UI-editable, persisted in the NetBox database) and read it via
`proxbox_api.runtime_settings.get_int / get_float / get_bool / get_str`, which already
resolves **env var (override) → `ProxboxPluginSettings` → built-in default** with a
5-minute settings cache (`proxbox_api/settings_client.py::get_settings`).

Runtime key disclosure requires the plugin's active superuser or explicit per-user
sensitive-data grant. Only HTTP 404 permits metadata compatibility; never
fallback after authorization, transport, or other runtime failures. Metadata
compatibility must strip encryption_key. Both requests share timeout allocation,
not a hard blocking-duration bound. Synchronous DNS, construction, and reads can
exceed it. The grant does not confer object visibility or provider/write authority.
Ordinary settings, caches, and overrides never retain encryption_key.
plugin_key_authority.py binds uncached runtime checks to immutable default-client
inputs and an opaque generation. Every plugin-root or Fernet acquisition checks
fresh authorization and exact settings-row visibility. Retired generations and
failed checks cannot use local, plaintext, development-seed, or alternate-client
fallback. Explicit reset and default-client reselection are required to replace
a retired source. Independent operator-selected keys retain their own cache.
Refuse circular encrypted-token bootstrap before creating a client. Keep
authority I/O outside locks and the event loop. The two-second authority window
rejects late results; it does not interrupt blocking DNS or reads. Caller
cancellation does not terminate a worker. See the configuration guide for
deadlines, transport bounds, compatibility, and memory-erasure limits.

Only fall back to a pure `.env` variable when the value is needed **before** the NetBox
connection exists or is **operator-only infrastructure** that has no business in the UI:
`PROXBOX_BIND_HOST`, `PROXBOX_DATABASE_PATH`, `PROXBOX_RATE_LIMIT`,
`PROXBOX_ENCRYPTION_KEY` / `PROXBOX_ENCRYPTION_KEY_FILE`, `PROXBOX_STRICT_STARTUP`,
`PROXBOX_SKIP_NETBOX_BOOTSTRAP`, `PROXBOX_GENERATED_DIR`,
`PROXBOX_RUNTIME_CODEGEN_ENABLED`,
`PROXBOX_CORS_EXTRA_ORIGINS`, `PROXBOX_SSH_KEY_DIR`, `PROXBOX_GUEST_AGENT_TIMEOUT`. Anything that controls sync behavior, batching,
concurrency, caching, or feature toggles belongs in `ProxboxPluginSettings`.

Do **not** invent shadow config layers (parallel JSON/YAML files, ad-hoc dotenv
sections, module-level constants meant as overrides) to dodge the migration cost.
If the new field needs the model + migration + form + serializer + template wiring on
the `netbox-proxbox` side, do all five — the existing fields in
`netbox_proxbox/models/plugin_settings.py` and migration
`0037_pluginsettings_runtime_tunables.py` show the pattern.

### Required in `.env` (process-level, no plugin-settings equivalent)

- `PROXBOX_BIND_HOST`: bind address used by the Docker `raw` and `granian` images (default: all IPv4 interfaces). Set to `::` for IPv4 + IPv6 dual-stack. The container entrypoints sanitize surrounding ASCII quotes/whitespace, so a Compose list-form value such as `- PROXBOX_BIND_HOST="::"` is tolerated even though the YAML quotes are NOT stripped. The `nginx` image listens on both stacks regardless of this variable.
- `PROXBOX_DATABASE_PATH`: optional SQLite database path override. Default is `/data/database.db` (a Docker volume mount point). Docker volumes should be mounted at `/data` to persist the database across container restarts and image upgrades. Production deployments can override this to `/var/lib/proxbox-api/database.db` if needed.
- `PROXBOX_RATE_LIMIT`: max API requests per minute per IP address (default: 300). Read at app construction.
- `PROXBOX_CORS_EXTRA_ORIGINS`: extra CORS origins (read at app construction).
- `PROXBOX_STRICT_STARTUP`: turns generated-route startup failures into fatal startup errors.
- `PROXBOX_SKIP_NETBOX_BOOTSTRAP`: skips default NetBox bootstrap at startup.
- `PROXBOX_GENERATED_DIR`: override output directory for the schema generator CLI (`proxbox-schema`); default is `$XDG_DATA_HOME/proxbox/generated/proxmox` (typically `~/.local/share/proxbox/generated/proxmox`).
- `PROXBOX_RUNTIME_CODEGEN_ENABLED`: development-only process opt-in for HTTP schema generation, runtime route refresh, user-generated schema discovery, and user-schema source rendering. Defaults to `false`; production must leave it disabled.
- `PROXBOX_ENCRYPTION_KEY`: secret key used to encrypt credentials (NetBox token, Proxmox password/token) at rest in the local SQLite database. SHA-256 derives the Fernet key. Initial selection uses the environment key, then the private generation-bound plugin runtime root, then an independently configured local file (default `<repo_root>/data/encryption.key`), then no source. Environment and local sources retain their cache. A selected plugin source requires fresh runtime authorization for every cryptographic acquisition; refusal or retirement does not permit fallback. A missing source blocks nonempty credential writes unless `PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS` explicitly enables lab-only plaintext storage. An encrypted service token without an independently available bootstrap key is refused before client construction.
- `PROXBOX_ENCRYPTION_KEY_FILE`: optional override for the local key file path used when neither the env var nor the plugin settings provide a key. Defaults to `<repo_root>/data/encryption.key`.
- `PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS`: explicit opt-in for plaintext credential storage. With no encryption key configured, credential **writes** (endpoint create/update that store a secret) are refused unless this is set to `1`/`true`/`yes`; reads and the rest of the service keep working. Use only in dev/tests.
- `PROXBOX_SSH_KEY_DIR`: directory prefix for private keys accepted by Cloud Image Build Pipeline remote execution (`ssh_identity_file`). Defaults to `/etc/proxbox/ssh_keys`; request paths must resolve under this directory before `ssh -i` is constructed.
- `PROXBOX_LOG_LEVEL`: console log verbosity (default `INFO`). Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (case-insensitive). Controls only the console handler; the in-memory buffer always receives DEBUG+ and the rotating file handler always writes WARNING+. Setting `DEBUG` also enables full `netbox_sdk.client` per-request tracing which is suppressed at all other levels to prevent INFO-level flooding.
- `PROXBOX_GUEST_AGENT_TIMEOUT`: per-call timeout in seconds (default `15`, range 1–600) for QEMU guest-agent `network-get-interfaces`. Environment-only; netbox-proxbox has no `guest_agent_timeout` field and plugin payloads drop that key. Restart required after change.
### Plugin-managed (env override optional, defaults shown)

Each maps to a key in `ProxboxPluginSettings` and can be edited from the NetBox plugin settings page.

| Env var | Plugin key | Default |
|---------|-----------|---------|
| `PROXBOX_NETBOX_TIMEOUT` | `netbox_timeout` | 120 s |
| `PROXBOX_NETBOX_MAX_CONCURRENT` | `netbox_max_concurrent` | 1 |
| `PROXBOX_NETBOX_MAX_RETRIES` | `netbox_max_retries` | 5 |
| `PROXBOX_NETBOX_RETRY_DELAY` | `netbox_retry_delay` | 2.0 s |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `vm_sync_max_concurrency` | 4 |
| `PROXBOX_FETCH_MAX_CONCURRENCY` | `proxbox_fetch_max_concurrency` | 8 |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `proxmox_fetch_concurrency` | 8 (4 in task-history) |
| `PROXBOX_SESSION_ACQUIRE_CONCURRENCY` | `session_acquire_concurrency` | 8 |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `netbox_write_concurrency` | 8 (4 in task-history/snapshots) |
| `PROXBOX_BACKUP_BATCH_SIZE` | `backup_batch_size` | 5 |
| `PROXBOX_BACKUP_BATCH_DELAY_MS` | `backup_batch_delay_ms` | 200 ms |
| `PROXBOX_BULK_BATCH_SIZE` | `bulk_batch_size` | 50 |
| `PROXBOX_BULK_BATCH_DELAY_MS` | `bulk_batch_delay_ms` | 500 ms |
| `PROXBOX_INTERFACE_BATCH_SIZE` | `interface_batch_size` | 5 |
| `PROXBOX_INTERFACE_BATCH_DELAY_MS` | `interface_batch_delay_ms` | 100 ms |
| `PROXBOX_NETBOX_GET_CACHE_TTL` | `netbox_get_cache_ttl` | 60 s (0 = disabled) |
| `PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES` | `netbox_get_cache_max_entries` | 4096 |
| `PROXBOX_NETBOX_GET_CACHE_MAX_BYTES` | `netbox_get_cache_max_bytes` | 52_428_800 (50 MB) |
| `PROXBOX_DEBUG_CACHE` | `debug_cache` | false |
| `PROXBOX_EXPOSE_INTERNAL_ERRORS` | `expose_internal_errors` | false |
| `PROXBOX_NETBOX_OPENAPI_PERSIST` | `netbox_openapi_persist` | true (disable to resolve the NetBox OpenAPI schema fully in-memory — no disk read/write; env or plugin-settings page) |
| n/a | `hardware_discovery_sync_nic_macs` | false (plugin-only; requires `hardware_discovery_enabled=true`; a missing field from an older plugin is false) |

### Task-history sync ownership

VM create routes expose `sync_task_history` with a backward-compatible default
of `true`. Standalone and targeted VM syncs run one scoped task-history
aggregate after the successful NetBox VM IDs are known. Full-update is the
single-owner exception: it passes `sync_task_history=false` into its VM stage,
then runs the dedicated task-history stage exactly once. Deploy backend support
before changing an orchestrating plugin to send `false`; older callers that
omit the flag remain compatible.

The task-history service walks each selected Proxmox node's archive with
`limit=500`, increasing `start` offsets, one fixed run-start `until`, and one
global fetch semaphore. It loads the typed VM sync-state sidecars once and
treats endpoint ID, cluster name, VMID, and VM type as authoritative identity;
custom fields are not an identity fallback. A malformed, missing, duplicate,
or unreadable sidecar for a relevant selected VM fails closed. Estate-wide runs
skip genuinely unmanaged VMs, while ownership collisions and cross-owner UPIDs
are skipped and mark the run degraded. UPIDs are deduplicated before one bulk
NetBox reconciliation, with no per-UPID status reads or per-record write
fallback.

Selected NetBox IDs are deduplicated and sent in chunks of at most 100 using
repeated `id` query values. NetBox list reads follow the server-provided `next`
links and reject repeated links or overlapping page content instead of
returning a partial set. A later-page or single-node failure retains safe rows
and reports `degraded=true`; VM-list failure, no usable selected nodes, total
node failure, or global reconciliation failure raises `ProxboxException`, so
REST and SSE cannot report a misleading success. The full operational contract
is documented in [`docs/sync/task-history.md`](docs/sync/task-history.md).

### Staged VM selection (strict vs lenient)

VM-scoped stages resolve each VM's Proxmox owner through
`services/sync/vm_filter.py` under an explicit `SelectionMode`. `STRICT` raises
HTTP 502 on the first VM whose ownership cannot be resolved and is used by the
single-VM path routes (`/{netbox_vm_id}/...` for VM sync, backups, snapshots,
and disks). `LENIENT` drops such a VM with a `WARNING` naming its NetBox id,
processes the rest, and reports `[{"netbox_vm_id", "reason"}]` warnings with
`degraded=true` (HTTP 200 / `ok=true`, never 502); it is used by
`netbox_vm_ids` list routes, `/all/create`, the estate interface and IP stages,
the full-backup cache, and both full-update variants. Routes choose by
addressing, so an older plugin that sends no new flag keeps working. A dropped
VM is never reconciled or covered by stale-backup or stale-snapshot cleanup, and
an unreadable sidecar scan, an invalid id, or a selection NetBox does not return
in full stays fatal in every mode. Task history is not part of this mode: its
explicitly selected VMs without identity remain fatal. The full contract, the
REST/SSE result shapes, and the full-update aggregation are in
[`docs/sync/workflows.md`](docs/sync/workflows.md#staged-run-selection-strict-versus-lenient-ownership).

### VM interface sync strategy

VM sync, VM-interface sync, and VM-IP sync routes accept
`vm_interface_sync_strategy`. The default `guest_os_model` keeps the core
NetBox `virtualization.VMInterface` named by Proxmox config (`net0`, `net1`,
...) and writes guest OS interfaces (`ens18`, `eth0`, ...) to the
netbox-proxbox plugin endpoints:
`/api/plugins/proxbox/guest-vm-interfaces/` and
`/api/plugins/proxbox/guest-vm-interface-addresses/`. Guest address rows must
reuse the existing core `ipam.IPAddress` IDs; do not create duplicate IPAM
records for the guest side. If older netbox-proxbox releases return 404 for
those plugin endpoints, log and skip the guest writes without failing core
interface/IP sync. Do not trust plugin endpoint filters blindly: before patching
a returned guest record, verify client-side that `GuestVMInterface` matches
`(virtual_machine, name)` and that guest address rows match
`(guest_interface, ip_address)`. If the endpoint returns a foreign record, log
and skip instead of patching it.

`legacy_rename` is deprecated and exists only for compatibility. It preserves
the old `use_guest_agent_interface_name=true` behavior that renames the core
VMInterface to the guest OS name and must emit the deprecation warning.

## Validation

Run these checks before pushing changes (the `rtk` prefix is a local token-saving alias around the underlying `uv run` commands; `uv run ruff check .` etc. are the canonical forms):

```bash
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall proxbox_api tests
uv run python -c "import proxbox_api.main"
uv run python -c "from proxbox_api.proxmox_to_netbox.proxmox_schema import load_proxmox_generated_openapi; assert load_proxmox_generated_openapi().get('paths')"
uv run ty check proxbox_api/types proxbox_api/utils/retry.py proxbox_api/schemas/sync.py proxbox_api/database_protocols.py proxbox_api/utils/async_compat.py proxbox_api/runtime_settings.py proxbox_api/settings_client.py proxbox_api/ceph/endpoint_binding.py proxbox_api/ceph/timing.py proxbox_api/ceph/v2_schemas.py proxbox_api/ceph/v2_engine.py proxbox_api/ceph/v2_routes.py proxbox_api/ceph/v2_providers/base.py proxbox_api/ceph/v2_providers/proxmox.py proxbox_api/ceph/v2_providers/proxmox_writer.py
uv run pytest tests
```

If you touch `proxbox_api/services/sync/reconciliation/`, `tests/reconciliation/`,
`benchmarks/reconciliation/`, `proxbox-reconcile-rs/`, or `.github/workflows/rust-reconcile.yml`,
also run the focused Rust/parity checks:

```bash
cargo test --no-default-features --manifest-path proxbox-reconcile-rs/Cargo.toml
uv pip install -e proxbox-reconcile-rs
PROXBOX_TEST_RECONCILIATION_ENGINE=compare \
  PROXBOX_TEST_RECONCILIATION_COMPARE_STRICT=true \
  uv run pytest tests/reconciliation -q
```

If you touch `nextjs-ui/`, also run:

```bash
cd nextjs-ui
npm run lint
npm run build
```

## Type System

The project uses Python's type hints with optional `mypy` checking. Type system conventions:

### Domain Types (Type Aliases)

Use `TypeAlias` for semantic clarity on primitive values:

```python
from proxbox_api.types import RecordID, VMID, ClusterName

def process_device(device_id: RecordID, cluster: ClusterName) -> None:
    """Type-safe device processing with semantic naming."""
```

### Protocols for Duck-Typing

Use `@runtime_checkable` Protocols when working with multiple object types:

```python
from proxbox_api.types import NetBoxRecord, SyncResult

def update_record(record: NetBoxRecord) -> SyncResult:
    """Works with any NetBox object having record interface."""
```

### TypedDicts for Data Structures

Use `TypedDict` when dictionary structure matters:

```python
from proxbox_api.types import VMPayloadDict, DevicePayloadDict

def build_vm_payload(...) -> VMPayloadDict:
    """Type-safe NetBox VM payload with documented fields."""
    return {
        "name": "vm-name",
        "cluster": cluster_id,
        "vcpus": 4,
    }
```

See `proxbox_api/types/CLAUDE.md` for complete typing guidelines.

## Rust-Python FFI Reference

The backend now has one optional native extension:
`proxbox-reconcile-rs`, a PyO3/maturin Rust package for the deterministic VM
operation-queue builder used by full VM sync. Python remains the default engine
because live `netbox.example.com` timing and synthetic benchmarks showed the
full Rust path was not faster after JSON/adaptation overhead.

The runtime seam is:

```
prepared_vms + netbox_snapshot + flags
  -> proxbox_api.services.sync.reconciliation.build_vm_operation_queue()
  -> Python engine, compare mode, or optional Rust engine
  -> CREATE | GET | UPDATE operations with patch_payload
```

Keep FastAPI routes, NetBox/Proxmox clients, SQLite, auth, retries, streaming,
and dispatch execution in Python. Only pure, synchronous, CPU-bound queue
construction belongs behind the Rust bridge.

Engine modes are selected through `ProxboxPluginSettings.reconciliation_engine`.
This selector is intentionally DB-backed through NetBox plugin settings, not a
backend environment-variable override.

- `reconciliation_engine=python`: default and production-safe path.
- `reconciliation_engine=compare`: run both engines, return Python,
  and report mismatches through logs and `proxbox_reconcile_mismatch_total`.
- `reconciliation_engine=rust`: return Rust output; requires the
  native package and should only be used after compare mode is clean.
- `reconciliation_compare_strict=true`: raise on mismatch in compare mode,
  intended for validation and local parity debugging.

When adding future native Rust extensions, use PyO3 as the binding framework:

- Architecture reference: `/root/personal-context/PYO3.md`
- Per-directory guidance: `/root/personal-context/pyo3/CLAUDE.md`
- Dashboard overview: `/pyo3` route on the personal-context app
- Build backend: maturin (preferred) or setuptools-rust
- Version: PyO3 v0.28.3, minimum Rust 1.83

Current Rust package:

- `proxbox-reconcile-rs/`: Rust VM reconciliation crate.
- `proxbox_api/services/sync/reconciliation/rust_bridge.py`: Pydantic v2
  JSON-byte adapter and optional native import.
- `proxbox_api/services/sync/reconciliation/vm_queue.py`: engine-neutral
  wrapper, Python fallback, compare mode, strict mode, mismatch diffing, and
  dataclass adaptation.
- `tests/reconciliation/`: Python contract, bridge, engine-mode, and
  Rust/Python parity fixtures.
- `.github/workflows/rust-reconcile.yml`: Rust unit tests, strict parity matrix,
  and wheel build matrix.

### Python synchronization performance direction

Keep Python as the production engine and pause expansion of the custom Rust
engine unless complete-operation measurements retain a substantial CPU
bottleneck after Python optimization. VM preparation validates Proxmox resource
and config inputs once and reuses the typed models. Name resolution completes
before final desired payloads are validated off the event loop and retained in
`PreparedVMState.desired_state`. The Python planner reuses that canonical model
and keeps the indexed endpoint/type identity algorithm.

Full-update config fetches use a fixed worker set with a pending queue capped at
twice `PROXBOX_VM_SYNC_MAX_CONCURRENCY`; do not replace it with one coroutine per
inventory row. Keep the full fetch/snapshot/name-resolution barriers, ordered
results, per-item failure accounting, and cancellation propagation. Detailed
batch logs separate wall time, process CPU, snapshot/hydration/name resolution,
canonicalization, reconciliation, dispatch, and persistence. Synthetic planner
benchmarks record commit/runtime metadata, CPU, and traced peak memory, but do
not substitute for staging request-count, upstream-latency, responsiveness,
retry, failure, and recovery evidence.

Candidate hotpaths for future Rust acceleration:
- `proxbox_api/proxmox_to_netbox/` — object mapping and field transformation
- `proxbox_api/proxmox_codegen/` — OpenAPI schema crawling and code generation
- `proxbox_api/services/sync/` — additional bulk reconciliation and diffing
- `proxbox_api/generated/` — generated Pydantic model validation

## Extension Rules

1. Update schemas and enums before route handlers.
2. Put reusable workflow logic in services, not routes.
3. Keep route modules focused on request orchestration and response shaping.
4. Add or update tests for new behavior.
5. Regenerate generated artifacts instead of editing them by hand.

## Branch Cleanup Policy

Always delete a feature branch (locally and on the remote) immediately after it
has been merged into its target branch. This applies to every branch — feature,
fix, security, chore, release-prep — and to merges done locally or via a pull
request.

After a merge:

1. Remove the task worktree first if one exists:
   `git worktree remove ../proxbox-api.worktrees/<slug>`.
2. Delete the local branch: `git branch -d <branch>` (use `-D` only if Git
   reports the branch as unmerged after you have confirmed it really is merged).
3. Delete the remote branch if it was ever pushed:
   `git push origin --delete <branch>`. If `git ls-remote --heads origin <branch>`
   returns nothing, the remote already has no copy and this step is a no-op.
4. Run `git fetch --prune` (or `git remote prune origin`) so stale
   `origin/<branch>` refs disappear from local listings.

Never leave merged branches lingering. The only branches that should persist
long-term are `main`, active release branches, and any branch the user has
explicitly asked to keep.

## Software Engineering Life Cycle Requirements

This section establishes project-wide quality standards derived from industry-standard software engineering practices. All changes must conform to these requirements before release.

### Requirements Traceability and Design Documentation

**Architectural Design:** The backend's architecture is documented across:
- **Route contracts** (`proxbox_api/routes/`, `.github/workflows/*.yml`) — API surface and CI/CD integration points
- **Service layers** (`proxbox_api/services/`) — subsystem decomposition and dependency definitions
- **Schema definitions** (`proxbox_api/schemas/`) — NetBox, Proxmox, and Firecracker payload contracts
- **Database models** (`proxbox_api/session/`, `proxbox_api/models/`) — state management and persistence

Changes to routes, services, or schemas MUST include an updated architecture note in the closest CLAUDE.md explaining:
- What interface or subsystem changed
- Why the change is necessary (traceability to an issue or feature)
- What downstream systems are affected (NetBox plugin, management frontend, Firecracker host-agents)
- Any breaking changes or version floor bumps

**Verification:** Before opening a PR, confirm:
1. Route contracts match their docstrings and `.openapi` metadata
2. All new schemas are documented in the nearest CLAUDE.md
3. Breaking changes to Proxmox/NetBox/Firecracker contracts are flagged in the PR description
4. The netbox-proxbox plugin compatibility floor is noted (version `X.Y.Z` or later required)

### Code Coverage and Quality Metrics

**Enforced Coverage Ratchet:** The required non-E2E core suite enforces at least
65.40% branch-inclusive coverage for the measured `proxbox_api/` source. The
reproducible baseline was 65.51% on 2026-07-17. Coverage must not fall below the
ratchet; 85% remains the long-term target rather than the current gate.

**Coverage Reporting:**
- Local: `uv run pytest tests/ -n auto --ignore=tests/e2e --ignore=tests/test_generated_proxmox_routes.py --cov=proxbox_api --cov-branch --cov-report=term-missing --cov-report=xml:coverage.xml`
- CI: the public GitHub `test` job enforces the threshold on protected branches,
  reports missing lines, and retains `coverage.xml`; a regression blocks merge.
  The checked-in Gitea workflows include untrusted validation plus audited
  package publication and deployment controls. Documentation must not expose
  private runner inventory, credentials, control-plane addresses, or secret values.
- Release validation: the TestPyPI and PyPI candidate jobs use two xdist
  workers with `--dist loadgroup` and retain `--durations=20`. Python 3.13
  remains the branch-coverage leg and uploads `coverage.xml` for 14 days
  without rendering the full terminal missing-lines report. Because coverage's
  `sysmon` core cannot measure branches on Python 3.13, these jobs use the
  default core instead of requesting `COVERAGE_CORE=sysmon` and falling back.
  Python 3.12 validates compatibility without duplicate coverage collection.
- Exclusions: only `proxbox_api/generated/` (machine-generated schema output) and
  `proxbox_api/e2e/` (support code exercised by the separate Docker E2E matrix).
- Included: database, code-generation, testing helpers used by the core suite,
  and all other first-party source remain measured.

**Uncovered Code:** If code cannot be easily covered, document the rationale with an inline comment:
```python
try:
    ...
except ConnectionError:  # pragma: no cover - occurs only on network outage
    pass
```

### Testing and Regression Requirements

**Test Suite:** All changes must include unit and integration tests:
- **Unit tests** (`tests/test_*.py`) — verify individual routes, schemas, and services
- **Integration tests** (`tests/integration/`) — verify backend + NetBox + Proxmox workflows end-to-end
- **Regression tests** — add a test that would fail on pre-fix code before implementing any fix

**Regression Testing:** Before release, run:
```bash
uv run pytest tests/ --timeout=60 -v --cov=proxbox_api --cov-branch --cov-report=term-missing
uv run pytest tests/reconciliation -q  # if you changed sync reconciliation
```
This verifies that no previously passing test was broken by the change.

**E2E Validation:** Changes to VM sync, reconciliation, Firecracker provisioning, or NetBox integration must be validated against the full E2E Docker stack:
```bash
docker compose -f e2e/docker/docker-compose.yml up --build -d
bash e2e/docker/wait-for-stack.sh
bash e2e/docker/smoke.sh
```

### Static Analysis and Quality Gates

**Ruff (Linting & Formatting):**
```bash
uv run ruff check .          # Detect errors, style violations, unused imports
uv run ruff format --check . # Enforce code formatting
```
All violations block CI. Fix before pushing.

**Type Checking (Pyright strict):**
```bash
uv run ty check proxbox_api/types proxbox_api/utils/retry.py proxbox_api/schemas/sync.py proxbox_api/database_protocols.py proxbox_api/utils/async_compat.py proxbox_api/runtime_settings.py proxbox_api/settings_client.py proxbox_api/ceph/endpoint_binding.py proxbox_api/ceph/timing.py proxbox_api/ceph/v2_schemas.py proxbox_api/ceph/v2_engine.py proxbox_api/ceph/v2_routes.py proxbox_api/ceph/v2_providers/base.py proxbox_api/ceph/v2_providers/proxmox.py proxbox_api/ceph/v2_providers/proxmox_writer.py
```
Type mismatches block merge. Use `# type: ignore` only with justification.

**Defect Categories Detected:**
- Undefined variables, imports, method/attribute access
- Unused imports and dead code
- Security: SQL injection, unsafe exec/eval, insecure deserialization
- Type mismatches (Pyright strict mode)
- Complexity and maintainability

**Pre-commit Enforcement:**
```bash
uv run python -m compileall proxbox_api tests
uv run ruff check . && uv run ruff format --check .
uv run ty check proxbox_api/types proxbox_api/utils/retry.py proxbox_api/schemas/sync.py proxbox_api/database_protocols.py proxbox_api/utils/async_compat.py proxbox_api/runtime_settings.py proxbox_api/settings_client.py proxbox_api/ceph/endpoint_binding.py proxbox_api/ceph/timing.py proxbox_api/ceph/v2_schemas.py proxbox_api/ceph/v2_engine.py proxbox_api/ceph/v2_routes.py proxbox_api/ceph/v2_providers/base.py proxbox_api/ceph/v2_providers/proxmox.py proxbox_api/ceph/v2_providers/proxmox_writer.py
uv run pytest tests --timeout=60
```
All checks MUST pass before committing.

### Configuration Control and Change Management

**Configuration Items:** The following are managed under strict change control:
- Backend version (`pyproject.toml` version, `proxbox_api/__init__.py` `__version__`)
- NetBox compatibility floor (`proxbox_api/constants.py` `MIN_NETBOX_VERSION`)
- Proxbox API contracts (route signatures, schema payloads, SSE/WebSocket events)
- Database schema and migrations (any model/SQLModel changes)
- Environment variable list (all new `.env` variables must be documented in CLAUDE.md)

**Change Control Process:**
1. **Before changing a configuration item**, document the change and impact in the repository's issue or pull request.
2. **After merging**, update the relevant CLAUDE.md file to document the new requirement or floor.
3. **Release notes** MUST include breaking changes (e.g., "requires NetBox ≥4.5.8").

**Version Management:** Follow PEP 440:
- Use `X.Y.ZrcN` for release candidates (TestPyPI validation only)
- Use `X.Y.Z` for official releases
- Use `X.Y.Z.postN` for bug-fix releases (never `twine --skip-existing`)

### Pre-Release Verification Checklist

**Before opening a release PR or tag, verify ALL of the following:**

- [ ] All requirements are implemented and verified in code
- [ ] Code passes pre-commit checklist (syntax, lint, type-check, tests)
- [ ] Branch-inclusive core-suite coverage meets the enforced 65.40% ratchet
      (`pytest-cov --cov-branch --cov-report=term-missing`); 85% remains the long-term target
- [ ] Regression testing passes (`pytest tests/ --timeout=60 -v`)
- [ ] E2E Docker stack validation is green (if touching sync/Firecracker/NetBox paths)
- [ ] Changelog (`docs/release-notes/version-X.Y.Z.md`) is complete
- [ ] Architecture documentation (CLAUDE.md files) is updated
- [ ] NetBox compatibility floor is documented (version `X.Y.Z` or later required)
- [ ] Proxbox API breaking changes (if any) are flagged for netbox-proxbox
- [ ] All CI checks are green (GitHub Actions)

**During release publishing**:

- [ ] Create an immutable public tag or GitHub Release (never force-push tags)
- [ ] Monitor the public GitHub Actions publication workflows
- [ ] Verify dist is live on PyPI and Docker Hub before declaring success
- [ ] Update netbox-proxbox compatibility floor if this release changes the API contract

---

## Release Procedure

Public releases use the workflows in `.github/workflows/`. Release candidates
publish to TestPyPI from immutable tags; final GitHub Releases publish to PyPI
and then trigger the public Docker image workflow. Never reuse or replace a
consumed version or tag.

Gitea package publication is a separate package-only control in
`.gitea/workflows/publish-gitea.yml`. Dispatch it only from canonical `main`
with an exact immutable tag. It must not mirror tags, create GitHub releases,
deploy services, or contact runtime environments. Registry artifacts are
verified against a canonical source-bound manifest before that repository-
linked manifest is published. Existing versions require explicit
`resume_existing=true` and exact byte equality; otherwise publish a new
fixed-forward version.
The verified manifest is the terminal public deployment handoff. An external
system must independently bind the immutable repository, tag, source commit,
package version, artifact sizes and digests, and required CI result before it
deploys. Authorization, environment selection, rollout, health, rollback,
audit retention, and replay prevention remain under the external control
plane's authority. The checked deployment workflow validates the authorized
identities and invokes fixed host entry points; it does not own credentials,
choose deployment policy, or implement host rollout machinery.

Production deployment is a separate authorized control in
`.gitea/workflows/deploy-production.yml`. A `develop` push deploys only staging.
A production dispatch must run from canonical `main`, defaults to the selected
immutable Gitea package, and permits `main_branch` only as an explicit override.
Preserve exact source, package, manifest, artifact, CI-status, authorization,
and run binding; claim the single-use authorization only after non-mutating
preflight; validate the signed host receipt before publishing completion
evidence; and remove claimed proof material on every exit path. The signed
authorization has no CI-bypass field, so production exposes no unsigned bypass.

| Trigger | Use for | Publishes to |

## Native OpenTelemetry

The backend, Firecracker host agent, and standalone Proxmox mock use `fastapi[standard]==0.142.2` and native FastAPI telemetry. Public defaults never select a collector endpoint. Operators opt into OTLP HTTP/protobuf export through standard `OTEL_*` environment variables before lifespan startup. Application factories accept keyword-only `telemetry` settings and explicit providers; set `auto_configure=False` if another library already owns environment export, and retain caller ownership of explicit provider shutdown. Do not add duplicate FastAPI/ASGI instrumentation. Preserve HTTP authentication, SSE, WebSocket admission, console relay, and lifecycle contracts when upgrading dependencies. Configuration and sensitive-error-log guidance are documented in both language versions of `docs/getting-started/configuration.md`.

Native exporter privacy processors redact concrete HTTP paths and query values and remove arbitrary exception messages and stack traces while retaining route templates and error classification. Install caller-owned log redaction before caller-owned exporters; existing exporter order is preserved. Keep the standalone mock privacy helper independent of `proxbox_api`.

## Merged Guidance (from former AGENTS.md)

# proxbox-api Agent Index

## Mounted Operation Inventory

Read `proxbox_api/operation_inventory/CLAUDE.md` and
`docs/operations/operation-inventory.md` before changing the offline inventory,
its explicit developer CLI, contract inputs, schemas, rendered tables, or
build-only documentation consistency hook. Preserve every ordered registration,
collision, WebSocket and generated version/alias. All twenty-two fixed feature
inputs remain required; they represent fifteen reachable states, and the
all-disabled state is unreachable. Never infer effects from HTTP methods or
equate successful generation with caller/effect readiness. Keep generated
artifacts under `contracts/`, not `docs/`, and regenerate them after changing
their complete source closure. No inventory module belongs in runtime startup.

## Proxmox Browser Console Sessions

Read [`docs/api/console-sessions.md`](docs/api/console-sessions.md) and `proxbox_api/routes/proxmox/CLAUDE.md` before changing `POST /proxmox/console/sessions`, `ConsoleSessionRequest`, `ConsoleSessionResponse`, `_request_console_proxy()`, `_console_ticket()`, `_console_port()`, `_build_ws_url()`, or `ProxmoxSession.get_websocket_auth()`. This route returns private, short-lived Proxmox transport material only to the trusted `trusted-relay-service` relay. Preserve the explicit QEMU/LXC mode matrix, local endpoint-ID meaning, stored TLS policy, full ticket encoding, exactly one API-token or password-session WebSocket authentication value, and the rule that tickets, upstream URLs, cookies, and authorization values never reach browser JavaScript or logs.

The standalone browser surface is distinct:
`POST /proxmox/console/browser-sessions` returns only `stream_token`,
`websocket_path`, `expires_at`, and `console_type`; WebSocket
`/proxmox/console/browser-stream` consumes it from exactly one
`proxbox-token.<stream_token>` offered protocol alongside `binary`; never put
the token in a URI or echo its protocol. Keep the complete private
payload Fernet-encrypted in shared SQLite, the token random and one-use, the
30-second TTL and active-count limits bounded, and consumption atomic across
workers. Require the exact validated HTTPS Origin and `binary` subprotocol.
Preserve the stored endpoint `verify_ssl` policy, mediate RFB 3.8 VNC
authentication server-side for QEMU noVNC, and keep all client errors, close
reasons, and logs secret-free. Disable ambient proxies and refuse every
upstream redirect before a second connection so credentials cannot be replayed.
QEMU terminal and LXC terminal relay frames
directly; LXC noVNC remains invalid. Both create and consume must retain the
inactive `console_relay_policy` seam for the pending RPC-only endpoint policy;
do not implement or activate that policy here. Require the current endpoint row
to be enabled at both standalone boundaries without changing the existing
service-only broker's behavior.
Preflight Fernet before requesting an upstream ticket, bracket IPv6 authorities,
validate the node path segment, validate the expiry index at startup, and use
one shared idle deadline refreshed by traffic in either direction.

Runtime code generation defaults to disabled. Only the explicit development
setting `PROXBOX_RUNTIME_CODEGEN_ENABLED=true` mounts the HTTP generation and
route-refresh endpoints or permits runtime user-schema discovery. With the
default setting, route registration, schema discovery, and Pydantic source
rendering use bundled schemas only; the user-generated directory may receive
only the derived route cache. Refresh a bundled tag, including `latest`, solely
by installing a replacement package that bundles the updated schema.

## Certified Stack Pairing

Current sibling-source development pairing: `netbox-proxbox 0.0.27rc4 ... proxbox-api 0.0.23 ... proxmox-sdk 0.0.15 ... netbox-sdk 0.0.13`.

Proxmox node Device names use the effective endpoint/global
`node_device_name_template`. Keep the Proxmox short node name in API paths and
typed sync-state identity; use the rendered name only for NetBox Device writes
and lookups.
This source-only tuple is not a published or certified runtime pairing.
`proxbox-api 0.0.21.post2` adds authenticated Proxmox console WebSocket handshakes,
typed-sidecar-only inventory state, NetBox 4.6.6 certification, strict Python
3.12/3.13 support, and a verified network-free production release context.

## Gitea Package Publication

## Staging and Production Deployment

`.gitea/workflows/deploy-production.yml` deploys `develop` to staging and
accepts production dispatches only from canonical `main`. Production defaults
to an exact immutable Gitea package; deploying the canonical `main` commit is
an explicit override. Preserve the exact authorization, source, package,
manifest, artifact, green-CI, and run bindings. The single-use authorization
must be claimed only after every non-mutating preflight passes, the host-issued
completion receipt must be signature-validated before publication, and claimed
proof material must be removed on every exit path. A manual dispatch against
`develop` is not a staging shortcut and must fail before sending authorization
material or invoking a host deploy command. The signed authorization has no CI
bypass field, so the workflow exposes no emergency `skip_ci_gate` input and
every production source must already have green CI.

## VM Interface Sync Strategy

VM sync routes accept `vm_interface_sync_strategy`. The default
`guest_os_model` keeps the core NetBox `virtualization.VMInterface` named by
Proxmox config (`net0`, `net1`, ...) and writes guest OS interface rows
(`ens18`, `eth0`, ...) through netbox-proxbox plugin endpoints. Guest address
rows must reference the already-reconciled core `ipam.IPAddress` IDs; never
create duplicate IPAM records for the guest side. If those plugin endpoints are
missing on an older netbox-proxbox release, log and skip guest writes without
failing core interface/IP sync.

VM create routes default `sync_task_history=true` for backward compatibility
and run one aggregate after successful VM IDs are known. Full-update passes
`false` to the VM stage and owns one dedicated task-history stage. Roll out the
backend first before an orchestrating plugin begins sending `false`.

Bulk task history is node-oriented: paginate each selected node archive with a
fixed run-start `until`, load the typed VM sync-state sidecar once, map by its
endpoint + cluster + VMID identity, deduplicate UPIDs, then issue one NetBox bulk
reconcile. A present malformed/duplicate sidecar for a relevant VM always fails closed. There is no
custom-field fallback. A successful estate scan skips unmanaged VMs,
but for task history explicitly selected VMs without identity remain fatal (it
has no lenient mode; the other VM-scoped stages do, see "Staged VM Selection"
below). Encode selected
NetBox IDs as repeated multi-value parameters in deduplicated groups of at most
100; comma text is invalid for `MultiValueNumberFilter`. Never restore per-VM
node scans, per-UPID status requests, or per-record NetBox fallback. Preserve
safe partial rows and report `degraded=true` for missing scopes, ownership
ambiguity, and repeated/no-progress archive pages. Standalone REST raises 502
for that degraded result after reconciliation; SSE exposes the degraded phase
summary. Raise `ProxboxException` at fatal identity, coverage, pagination, or
reconcile boundaries so REST/SSE ends with `ok=false`.

Shared NetBox list traversal follows the server `next` URL with repeated query
values intact. Malformed pagination objects/links, empty+next pages, and any
record overlap fail closed. The 10,000-page/1,000,000-record hard bounds and any
caller offset/record cap raise HTTP 502 before another over-bound request; never
return or cache partial data. Omitted `netbox_vm_ids` means all, but present
empty/malformed selectors are HTTP 422. VM, backup, snapshot, and disk lookups
use deduplicated repeated-ID chunks of at most 100 and propagate lookup failure.

`proxbox_api/services/sync/vm_filter.py` resolves a VM's Proxmox owner from its
typed sidecar (endpoint, cluster, VMID, type) and the live resources. Its
`SelectionMode` decides what an unresolvable VM does. `STRICT` (the default of
the shared functions) raises HTTP 502 on the first one; routes that address one
VM by path (`/{netbox_vm_id}/...` for VM sync, backups, snapshots, and disks)
pass it explicitly. `LENIENT` drops the VM with a `WARNING` naming its NetBox id
and the reason, processes the rest, and returns the drops on the result's
`skipped` attribute as `[{"netbox_vm_id", "reason"}]`. Staged and estate runs
(`netbox_vm_ids` lists, `/all/create`, estate interface/IP stages, the full
backup cache, both full-update variants) are lenient. The mode is a plain kwarg
on internal functions, never a new `Query` parameter on a route function that
`full_update` calls directly.

- Lenient drops: incomplete or duplicated sidecar, an explicitly selected VM with
  none, no available/ambiguous cluster source, endpoint mismatch, no or several
  live resources, and shared-owner claims (all claimants dropped). Unmanaged VMs
  in an estate scan are skipped silently. An unreadable sidecar scan, an invalid
  id, and a selection NetBox does not return in full stay fatal in every mode.
- Never let a dropped VM reach a write or cleanup: it is absent from the backup
  ownership cache and never scanned for snapshots, so stale-row deletion cannot
  cover it. Tests pin this for backups and snapshots.
- Report drops through `services/sync/stage_result.py`: dict results get
  `degraded` and `warnings`; list results become `WarningList` (`.warnings`),
  which REST wraps as `{<stage>: [...], count, warnings, degraded}` only when
  degraded and SSE surfaces as `warnings` plus `degraded` (the `virtual-machines`
  `/create` result follows the same wrap; never write `or []` over a stage
  result, since an empty result that carries warnings is falsy). Lenient drops are
  HTTP 200 / `ok=true`, not 502. `full_update` aggregates every stage's warnings
  (tagged with `phase`) and sets top-level `degraded`.
- `write_virtual_machine_sync_state` logs a warning when the live endpoint,
  cluster, VMID, or type is missing or `unknown`; it never changes what is
  persisted.

## Required Checks

Run these before pushing anything that touches the backend package:

Keep Python as the production reconciliation engine. VM preparation reuses one
validated resource/config model pair, finalizes canonical desired state only
after name resolution, and feeds that model to the indexed Python planner.
Config fetch uses fixed workers and a pending queue bounded at twice
`PROXBOX_VM_SYNC_MAX_CONCURRENCY`; preserve ordering, full phase barriers,
per-item failures, cancellation, write authority, recovery, and persistence.
Benchmark fixtures must retain `sync_state_fields` so endpoint-first identity is
actually exercised. Treat synthetic timings as planner evidence only; cache,
executor, and concurrency changes require complete staging measurements.

If you edit `proxmox-mock/` (the local `proxmox-mock-api` dev package), run its own tests inside that directory. Note: `proxmox-sdk` is an **external pinned package** (`proxmox-sdk==0.0.15`); there is no local `proxmox-sdk/` subdirectory in this repo.

SDN support lives in `proxbox_api/routes/proxmox/sdn.py` and
`proxbox_api/services/sync/sdn.py`. Keep it read-only against Proxmox: the
`GET /proxmox/sdn/create/stream` stage may reconcile NetBox L2VPN,
L2VPNTermination, RouteTarget, Prefix, plugin metadata objects, and optional
`netbox_bgp` peer-group/session/routing-policy/prefix-list projections when
`sync_mode_sdn_bgp` is `always` or `bootstrap_only`, but it must not apply,
rollback, lock, or mutate Proxmox SDN configuration. Unsupported older clusters
and missing optional `netbox_bgp` APIs should emit skipped warnings rather than
failing healthy endpoints.

Ceph v2 writes live in `proxbox_api/ceph/`. Every Proxmox plan/approval/apply
must name one durable local endpoint and create one private full-schema
HMAC-bound session; generic selectors/session lists and first-session fallback
are forbidden. Bind every non-noop operation to one exact persisted node; never
select the first node or invent `localhost`. Strictly validate the payload for
the exact `(kind, action)` during planning and again at dispatch; reject unknown
or missing fields instead of filtering them. Persist/digest the plan plus a
stable server-keyed endpoint configuration revision, bind that revision through
approval/run records, and reject same-ID retargeting. Require a distinct
delegated actor to issue one hashed/expiring/single-use approval, consume it
atomically, append a live `dispatching` intent before every SDK call, and reload
`enabled`, `allow_writes`, revision, endpoint/session binding, and node
immediately before every mutation. Fetch and validate live `cluster/status`
node membership first, then verify the endpoint/session through a dedicated
uncached gate session; bootstrap-cached node membership is never write
authority. Keep durable audit/lease work on an independent request session so
heartbeats continue during slow gates, then require a fresh owner/expiry CAS
after preparation and before invoking the provider mutation. Renewal and
checkpoint predicates use database wall-clock time evaluated after row-lock
waits, so delayed statements cannot reclaim expired authority. Every live
checkpoint must retain the same unexposed lease-owner nonce and non-expired
lease. For task-based
mutations, UPID means submitted until terminal polling; atomically claim exactly
one provider-globally unseen complete UPID whose returned and embedded nodes equal the
plan node. Only `flag:create/update/delete` and `osd:update` are SDK-proven
synchronous completions; no other missing task ID means success. Shield task
claim/submission, synchronous-completion, and cancellation checkpoints through
repeated cancellation until the inner durability task finishes, then propagate
the remembered cancellation. Refuse startup with
`ceph_provider_task_claim_cross_endpoint_collision` rather than selecting or
discarding ambiguous cross-endpoint legacy evidence; this migration failure is
fatal and must stop route mounting. Resolve bounded Ceph task timeout, poll
interval, and run lease once off-loop as env override → plugin setting →
default, normalize poll interval to at most timeout, persist the immutable lease
duration, and bound every task-status call and sleep by the remaining deadline.
Missing/multiple/reused or node-inconsistent task IDs, expired run leases, crashes, and cancellation become
`outcome_unknown` and are not retried or overwritten by a late worker. Recursively
redact normalized secret aliases, exception values, and non-JSON fallback text
across persistence, API, SSE, every handler, and the DEBUG admin buffer.
`netbox-ceph` must resolve the plugin
endpoint to the canonical proxbox-api endpoint ID; a plugin PK is never a
substitute. Ceph
writes remain default-off unless both `PROXBOX_ENABLE_CEPH_V2_WRITES=true` and
`PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY=true`; the trusted authenticated gateway
must overwrite `X-Proxbox-Actor`. Legacy confirmation and non-Proxmox apply stay
closed; Dashboard/external apply and destructive capabilities remain false until
durable provider authority exists; reconcile stays read-only. Run the focused Ceph
security/concurrency/migration suites and keep
`docs/operations/ceph-write-approvals.md` plus its Portuguese translation
aligned.

Fix failures locally before finishing the task.

## VM Platform From The Guest OS

The `ostype` -> platform table in `proxmox_to_netbox/guest_os.py` is **data**: add a
guest type there, not in code. An unmapped `ostype` returns `None`, meaning *leave the
platform unset* — never guess an operating system onto an inventory page.

The guest-agent refinement is opt-in (`sync_vm_platform_from_guest_agent`, default
false) because it costs one Proxmox request per VM. Gate it on eligibility the sync
already knows — QEMU, running, `agent` enabled — before spending the request. Use
`name` + `version-id`, never `pretty-name`: the patch level would mint a new NetBox
platform on every minor update.

`platform_from_guest_agent()` reads data produced by a guest the operator may not
control. It must stay total: non-dict payloads, missing keys, wrong types, and oversized
strings all return `None`. `ensure_vm_platform()` is total too — it swallows upsert
failures and returns `None`. A blank inventory field must never cost a VM its sync.

Platform is set when a VM is created. Existing VMs are patched only when
`SyncOverwriteFlags.overwrite_vm_platform` is explicitly true; its default is false so
operator-managed NetBox assignments remain unchanged. The flag is part of the
CI-enforced cross-repo contract (`contracts/overwrite_flags.json`, mirrored in
netbox-proxbox's `constants.OVERWRITE_FIELDS`) and must remain aligned with the plugin's
global setting and per-endpoint tri-state override. Do not change its name, order, or
default in only one repository.

## Public Repository Boundary

Keep public documentation limited to contracts a public contributor can inspect and
run from this repository: `.github/workflows/` publication, the checked Gitea
publication and deployment controls, package/runtime configuration, and public API
behavior. The deployment workflow may encode the audited validation protocol needed
to fail closed, but documentation must not reproduce private runner inventories,
credentials, concrete control-plane addresses, or secret values.

## VM Description and Comments

The Proxmox VM note drives the NetBox `description`; the
`Synced from Proxmox node {node}` string is only the fallback for a note that is
absent, blank, or nothing but a `netbox-metadata` fence. The complete note goes to
`comments` when it carries more than the description does. Derive both through
`proxmox_to_netbox/description_metadata.py::derive_description_and_comments` — never
inline the placeholder or the 200-character rule in a payload builder. All three
builders (bulk stage, per-VM sync, VM-create service) must call it; they previously
each had their own copy and behaved three different ways.

`netbox-metadata` fences are stripped unconditionally, with
`parse_description_metadata` on or off — that flag governs the fenced block's PK
overrides only. Both fields ride the existing `overwrite_vm_description` gate; do not
add a separate `overwrite_vm_comments` flag, because the plugin cannot yet send one and
the content is the same under the same consent. When adding a field to the VM create
body, also add it to `normalize_current_virtual_machine_payload()` or the reconciler
diff will never patch it.

## NetBox Sync-State Lifecycle

Proxbox no longer creates, reconciles, reads, or writes NetBox custom fields.
The typed netbox-proxbox `/api/plugins/proxbox/sync-state/*` sidecar API is the
only reflection-state store. VM identity, run IDs, device and cluster timestamps,
VM-interface bridge foreign keys, and virtual-disk storage foreign keys must be
built from the live synchronization values. Sidecar writes remain best-effort:
404/501 responses from older plugin builds and transient NetBox errors are logged
and skipped without aborting sync. Sync reads use
`proxbox_api/services/sync/sync_state_reader.py` and never fall back to custom fields.

Role ownership uses the typed VM-sidecar
`proxmox_last_synced_role_id` field first. Full sync loads these snapshots once
and applies the decision after the Python/Rust queue seam; individual and
adoption paths use the same truth table. Persist ownership evidence only after
a successful reconcile.
Unavailable, failed, or conflicting reads preserve the role without claiming
ownership. Required ownership writes retry three times. After an exhausted
response, the backend authoritatively re-reads the typed snapshot, accepts a
confirmed commit, or restores and verifies both the previous role and snapshot
before surfacing VM failure. This prevents response loss from creating a false
operator lock on the next pass.

## Code Quality Standards
- API route signatures and schemas (backward-compatibility impact)
- Database schema (any SQLModel/model changes require migrations)
- Environment variable additions (document in CLAUDE.md)

### Firecracker Cloud Invariants

If your change touches Cloud provisioning:
1. Verify the host-agent provisioning contract is documented
2. Confirm `FirecrackerMicroVM` rows use `kind="firecracker"` and `instance_ref="firecracker:<id>"`
3. Check that provisioning streams conform to the management backend contract
4. Validate that netbox-proxbox inventory calls are compatible with the current plugin version

Violating these invariants breaks production cloud provisioning.

## Configuration policy

See `CLAUDE.md → Environment Variables → Adding a new tunable` for the full keep-list
and resolution-order details.

## Database Startup Boundary

`proxbox_api/database.py` resolves one absolute SQLite target during FastAPI
lifespan startup. `PROXBOX_DATABASE_PATH` is canonical when explicitly
configured; an absolute SQLite `DATABASE_URL` is compatible, but both operator
settings must normalize to the same file
when supplied together. Relative/in-memory targets and cwd fallback are
forbidden, and every raw `?` delimiter in `DATABASE_URL` is rejected. Apply the
legacy API-key-history guard to default and explicit targets; the exact-value
`PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY=1` escape is restricted to an
isolated, audited fresh-control-plane startup and must be removed after first-key
registration. It is atomically consumed by a durable sibling marker before
database writes; never delete that marker to re-arm bootstrap. Inaccessible
legacy candidates are fatal. Recovery requires explicit `UVICORN_WORKERS=1`;
multi-worker or unspecified recovery must fail before writes. The target's persistent sibling `.startup.lock`
serializes WAL probe, engine/table creation, fatal schema inspection, and all
migrations across processes; the required endpoint-table read must then pass
before readiness. Consumers use `get_engine()` / `get_async_sessionmaker()` after
startup; do not restore import-time engine construction, split the serialized
startup boundary, or downgrade database configuration/startup failures.

Physical-NIC MAC reflection is a native NetBox write and therefore uses its own
plugin-only opt-in, `hardware_discovery_sync_nic_macs` (default `false`), in
addition to the `hardware_discovery_enabled` master gate. Treat a missing field
from an older netbox-proxbox release as `false`; both flags must be true before
creating `dcim.MACAddress` rows or assigning `primary_mac_address`.

## Firecracker Cloud

Firecracker provisioning lives in `proxbox_api/routes/cloud/firecracker.py`,
`proxbox_api/firecracker_agent/`, and `proxbox_api/schemas/firecracker.py`.
The management backend resolves NetBox Proxbox host/image inventory and creates the
`FirecrackerMicroVM` row, then calls this backend at
`POST /cloud/firecracker/provision` or
`POST /cloud/firecracker/provision/stream`. This repo owns the host-agent HTTP
contract only; NetBox inventory remains in `netbox-proxbox`.
`host_agent_base_url` is still supplied by the caller after that inventory
resolution, but proxbox-api validates it before any outbound request: only
`http`/`https` URLs with a host, no embedded credentials, no query/fragment, and
a host accepted by the shared SSRF guard are allowed. Streamed failures return a
generic browser-visible error unless `PROXBOX_EXPOSE_INTERNAL_ERRORS=true`.

## QEMU Cloud-Init Templates

Live QEMU Cloud-Init template discovery lives in
`proxbox_api/routes/cloud/qemu_templates.py` and is mounted as
`GET /cloud/vm/templates?endpoint_id=<ProxmoxEndpoint id>`. It enumerates
Proxmox cluster resources for the selected endpoint, filters QEMU VM templates,
reads each template config, and returns only templates with a Cloud-Init drive
or `cicustom` metadata by default. The route is read-only and is consumed by
the management backend's `/cloud/vm/templates` route for the VM creation UI.

QEMU provisioning (`POST /cloud/vm/provision` and the SSE variant) accepts
optional `sockets`, `bridge`, `vlan_tag`, and `disk_gb` fields. These are
applied through the Proxmox API during the clone configuration flow; no direct
`qm` shell path is used for VM provisioning.

Cloud-image catalog invariant: Proxmox VE products must use the
`proxmox_iso` provider with official Proxmox VE installer ISO media. Do not
offer or accept `debian_cloud_image` for PVE catalog builds. Generated PVE
installer/template setup must use a graphical VGA display for noVNC; reserve
`serial0` + `vga serial0` for products that intentionally ship serial appliance
images, currently pfSense and OPNsense.

The Cloud Image Build Pipeline's SSH execution path sets `qm ... --agent
enabled=1` before converting the VM to a template, so clones inherit the
Proxmox-side QEMU guest agent setting.

Cloud Image Pipeline hardening invariants: derive snippet/storage readiness
from the resolved provider; stage every provider in randomized private
`/var/tmp` directories; resolve ISO/snippet paths from exact `pvesm path`
volume IDs; encode generated file content instead of interpolating it into
shell delimiters; use only server-owned, canonical-root, root-verified source
recipes and treat caller paths as assertions; preserve the
legacy `local-lvm` destination when storage is omitted; reject explicit-null
endpoint `ssh_port`; and invoke absolute SSH binaries with ambient config and
proxies disabled. Generic request-validation 422 responses must never reflect
Pydantic input or cloud-image secrets. SSH normalization belongs in the
route-neutral `schemas/cloud_image_security.py` boundary.

Execution rules:

- `PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION=true` is mandatory for remote execution.
- The checked-in netbox-packer-shaped fixture is producer-owned compatibility
  intent, not downstream validation. Keep the execution flag unset/false until
  a compatible netbox-packer release with endpoint-bound authorization is
  deployed and validated against the released proxbox-api contract. Enabling
  execution does not replace either endpoint write gate.
- `endpoint_id` is required when `execute=true`; requests without it fail closed
  with 422 before a script is rendered or SSH is attempted.
- The route runs `_gate()` first so `ProxmoxEndpoint.allow_writes=True` is
  required, then `_packer_template_builds_gate()` so the separate default-off
  `allow_packer_template_builds=True` capability is required, then
  `gate_ssh_access()` so `access_methods="api_ssh"` is required before
  resolving execution authority. The endpoint must also be enabled and
  carry a complete persisted binding (`ssh_target_node`, `ssh_host`,
  `ssh_username`, `ssh_port`, `ssh_identity_file`,
  `ssh_known_host_fingerprint`). Derive execution exclusively from that row;
  caller SSH fields are compatibility assertions and any mismatch must fail.
  Verify the persisted host-key fingerprint and pass the exact scanned key to
  OpenSSH with strict host checking before the isolated systemd unit starts.
  Open the identity once with `O_NOFOLLOW`, verify the descriptor with `fstat`
  as a root/service-owned regular file with no group/world permissions, and
  inherit that descriptor through `/proc/self/fd`; never reopen the mutable key
  pathname in an SSH child.
- Require the signed, five-minute `preflight_plan_token` produced for the exact
  server-rendered, domain-separated HMAC `recipe_digest`. Endpoint configuration
  uses a separate keyed binding. Revalidate endpoint configuration, target,
  storage, VMID, and recipe; rerun preflight; authoritatively refresh and
  revalidate the endpoint again immediately before consuming the plan; and
  acquire the durable unique `endpoint_id:vmid` blocker before SSH.
- Execute asynchronously in a unique server-generated `systemd-run` unit,
  continuously draining stdout/stderr into counters without retaining output.
  Support timeout, request, and operator cancellation. A zero exit code is not
  success until the final Proxmox API artifact check passes; preserve unknown
  or partial state as `recovery_required` and never auto-delete it. Recovery,
  cancellation, unknown state, and lease expiry retain the blocker until an
  explicit reconciliation workflow exists. Keep mandatory cleanup, journal
  updates, and session close alive through repeated cancellation, and use
  compare-and-swap journal transitions so stale cancel/completion requests do
  not overwrite the winning state.

Read-only preflight and response privacy rules:

- `POST /cloud/templates/images/preflight` v1 resolves the exact enabled
  persisted endpoint to exactly one database-backed session; never select the
  first session or use a write gate as the resolver.
- Preflight uses GET-only node/storage/VMID checks and must work when
  `allow_writes=False`. Malformed collections and missing `enabled`/`active`
  storage state fail closed as `unsupported`. Use the normalized target:
  image storage requires `iso` only for `proxmox_iso`; release/source providers
  use private staging. VM storage requires `images`, and snippet storage is
  checked only when the provider-derived plan needs it.
  `cluster/nextid?vmid=` is authoritative; resource enumeration is supplemental
  and cannot turn a denied/malformed probe into success. `content=import` is the
  separate download-url POST value, not a configured storage capability.
- Preflight session creation uses the minimal authenticated SDK mode and must
  not trigger generic cluster/join/fingerprint discovery. A v1 readiness caller
  may omit `recipe_digest`; only a digest-bound request can receive an
  executable signed plan, and plan issuance itself remains database-read-only.
- Findings contain only `code`, `severity`, `target`, and `message`. Session
  creation/upstream failures must be fixed diagnostics without credentials or
  raw exceptions in responses or logs.
- Build response v2 omits URLs, cloud-init, scripts, commands, stdout, and
  stderr by default and during execution. Sensitive preview requires both
  `execute=false` and `include_sensitive_preview=true` and must never be logged
  or persisted. Unexpected execution/direct-SDK/cleanup exception text must be
  normalized into fixed diagnostics and type-only application logs. Tests must
  cover cleanup failures and cancellation so this remains evidence, not an
  assumption.
- Preflight v1 and build response v2 remain supported through `0.0.21.x`; a
  breaking replacement is no earlier than `v0.0.22.0` and must be documented.
  During that window, accept `storage` only as a compatibility alias for the
  canonical `vm_storage`; reject conflicts and do not emit `storage` in OpenAPI.

## Azure VHD Import Pipeline

Azure managed-disk V2V planning/execution lives in
`proxbox_api/routes/cloud/azure_vhd_imports.py` and
`proxbox_api/routes/cloud/azure_vhd_pipeline.py`, mounted as
`POST /cloud/azure/vhd-imports`. The route validates an
`AzureVhdImportRequest`, renders the exact `curl` + `qemu-img convert` +
`qm create` + `qm importdisk` script, and optionally runs it over SSH when
`execute=true`.

Execution rules:

- `PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION=true` is mandatory for remote execution.
- `endpoint_id` is required in execute mode so `_gate()` can enforce
  `ProxmoxEndpoint.allow_writes`.
- The generated script preflights the SSH destination node name, VMID
  availability, target storage, bridge presence, and required host tooling
  before downloading the VHD.
- The download is resumable (`curl -C -`), both source and converted images are
  checked with `qemu-img info`, and the imported disk volid is parsed from
  `qm importdisk` output instead of guessed from `pvesm list`.
- Linux uses `virtio-scsi-single` + `scsi0`; the Windows-safe profile uses
  `sata0` + `e1000` for first boot before VirtIO drivers are installed.
- The route is consumed by the management admin page
  `/cloud/azure-to-proxbox-migration`.

## Primary Guide

- `CLAUDE.md`

## Scoped Guides

### Top-level packages
- `proxbox_api/CLAUDE.md`
- `proxbox-reconcile-rs/CLAUDE.md`
- `proxbox-reconcile-rs/AGENTS.md`
- `proxmox-mock/CLAUDE.md` (local dev mock; `proxmox-sdk` is an external PyPI package)
- `nextjs-ui/CLAUDE.md`
- `nextjs-ui/AGENTS.md`

### Infrastructure
- `.github/CLAUDE.md`
- `docker/CLAUDE.md`
- `docs/CLAUDE.md`
- `tests/CLAUDE.md`
- `scripts/CLAUDE.md`
- `tasks/CLAUDE.md`
- `automation/CLAUDE.md`
- `proxmox-mock/CLAUDE.md`

### proxbox_api subpackages
- `proxbox_api/app/CLAUDE.md`
- `proxbox_api/routes/CLAUDE.md`
- `proxbox_api/routes/cloud/CLAUDE.md`
- `proxbox_api/routes/cloud/firecracker.py`
- `proxbox_api/routes/admin/CLAUDE.md`
- `proxbox_api/routes/dcim/CLAUDE.md`
- `proxbox_api/routes/extras/CLAUDE.md`
- `proxbox_api/routes/netbox/CLAUDE.md`
- `proxbox_api/routes/proxbox/CLAUDE.md`
- `proxbox_api/routes/proxbox/clusters/CLAUDE.md`
- `proxbox_api/routes/proxmox/CLAUDE.md`
- `proxbox_api/routes/sync/CLAUDE.md`
- `proxbox_api/routes/virtualization/CLAUDE.md`
- `proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md`
- `proxbox_api/services/CLAUDE.md`
- `proxbox_api/services/sync/CLAUDE.md`
- `proxbox_api/services/sync/reconciliation/CLAUDE.md`
- `proxbox_api/services/sync/individual/CLAUDE.md`
- `proxbox_api/session/CLAUDE.md`
- `proxbox_api/schemas/CLAUDE.md`
- `proxbox_api/schemas/firecracker.py`
- `proxbox_api/schemas/netbox/CLAUDE.md`
- `proxbox_api/schemas/netbox/dcim/CLAUDE.md`
- `proxbox_api/schemas/netbox/extras/CLAUDE.md`
- `proxbox_api/schemas/netbox/virtualization/CLAUDE.md`
- `proxbox_api/schemas/virtualization/CLAUDE.md`
- `proxbox_api/enum/CLAUDE.md`
- `proxbox_api/enum/netbox/CLAUDE.md`
- `proxbox_api/enum/netbox/dcim/CLAUDE.md`
- `proxbox_api/enum/netbox/virtualization/CLAUDE.md`
- `proxbox_api/proxmox_codegen/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md`
- `proxbox_api/generated/CLAUDE.md`
- `proxbox_api/generated/netbox/CLAUDE.md`
- `proxbox_api/generated/proxmox/CLAUDE.md`
- `proxbox_api/types/CLAUDE.md`
- `proxbox_api/utils/CLAUDE.md`
- `proxbox_api/custom_objects/CLAUDE.md`
- `proxbox_api/diode/CLAUDE.md`
- `proxbox_api/e2e/CLAUDE.md`

## CLAUDE.md Index

- [.github/CLAUDE.md](.github/CLAUDE.md)
- [CLAUDE.md](CLAUDE.md)
- [automation/CLAUDE.md](automation/CLAUDE.md)
- [docker/CLAUDE.md](docker/CLAUDE.md)
- [docs/CLAUDE.md](docs/CLAUDE.md)
- [nextjs-ui/CLAUDE.md](nextjs-ui/CLAUDE.md)
- [proxbox_api/CLAUDE.md](proxbox_api/CLAUDE.md)
- [proxbox_api/app/CLAUDE.md](proxbox_api/app/CLAUDE.md)
- [proxbox_api/custom_objects/CLAUDE.md](proxbox_api/custom_objects/CLAUDE.md)
- [proxbox_api/diode/CLAUDE.md](proxbox_api/diode/CLAUDE.md)
- [proxbox_api/e2e/CLAUDE.md](proxbox_api/e2e/CLAUDE.md)
- [proxbox_api/enum/CLAUDE.md](proxbox_api/enum/CLAUDE.md)
- [proxbox_api/enum/netbox/CLAUDE.md](proxbox_api/enum/netbox/CLAUDE.md)
- [proxbox_api/enum/netbox/dcim/CLAUDE.md](proxbox_api/enum/netbox/dcim/CLAUDE.md)
- [proxbox_api/enum/netbox/virtualization/CLAUDE.md](proxbox_api/enum/netbox/virtualization/CLAUDE.md)
- [proxbox_api/generated/CLAUDE.md](proxbox_api/generated/CLAUDE.md)
- [proxbox_api/generated/netbox/CLAUDE.md](proxbox_api/generated/netbox/CLAUDE.md)
- [proxbox_api/generated/proxmox/CLAUDE.md](proxbox_api/generated/proxmox/CLAUDE.md)
- [proxbox_api/proxmox_codegen/CLAUDE.md](proxbox_api/proxmox_codegen/CLAUDE.md)
- [proxbox_api/proxmox_to_netbox/CLAUDE.md](proxbox_api/proxmox_to_netbox/CLAUDE.md)
- [proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md](proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md)
- [proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md](proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md)
- [proxbox_api/routes/CLAUDE.md](proxbox_api/routes/CLAUDE.md)
- [proxbox_api/routes/admin/CLAUDE.md](proxbox_api/routes/admin/CLAUDE.md)
- [proxbox_api/routes/dcim/CLAUDE.md](proxbox_api/routes/dcim/CLAUDE.md)
- [proxbox_api/routes/extras/CLAUDE.md](proxbox_api/routes/extras/CLAUDE.md)
- [proxbox_api/routes/netbox/CLAUDE.md](proxbox_api/routes/netbox/CLAUDE.md)
- [proxbox_api/routes/proxbox/CLAUDE.md](proxbox_api/routes/proxbox/CLAUDE.md)
- [proxbox_api/routes/proxbox/clusters/CLAUDE.md](proxbox_api/routes/proxbox/clusters/CLAUDE.md)
- [proxbox_api/routes/proxmox/CLAUDE.md](proxbox_api/routes/proxmox/CLAUDE.md)
- [proxbox_api/routes/sync/CLAUDE.md](proxbox_api/routes/sync/CLAUDE.md)
- [proxbox_api/routes/virtualization/CLAUDE.md](proxbox_api/routes/virtualization/CLAUDE.md)
- [proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md](proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md)
- [proxbox_api/schemas/CLAUDE.md](proxbox_api/schemas/CLAUDE.md)
- [proxbox_api/schemas/netbox/CLAUDE.md](proxbox_api/schemas/netbox/CLAUDE.md)
- [proxbox_api/schemas/netbox/dcim/CLAUDE.md](proxbox_api/schemas/netbox/dcim/CLAUDE.md)
- [proxbox_api/schemas/netbox/extras/CLAUDE.md](proxbox_api/schemas/netbox/extras/CLAUDE.md)
- [proxbox_api/schemas/netbox/virtualization/CLAUDE.md](proxbox_api/schemas/netbox/virtualization/CLAUDE.md)
- [proxbox_api/schemas/virtualization/CLAUDE.md](proxbox_api/schemas/virtualization/CLAUDE.md)
- [proxbox_api/services/CLAUDE.md](proxbox_api/services/CLAUDE.md)
- [proxbox_api/services/sync/CLAUDE.md](proxbox_api/services/sync/CLAUDE.md)
- [proxbox_api/services/sync/reconciliation/CLAUDE.md](proxbox_api/services/sync/reconciliation/CLAUDE.md)
- [proxbox_api/services/sync/individual/CLAUDE.md](proxbox_api/services/sync/individual/CLAUDE.md)
- [proxbox_api/session/CLAUDE.md](proxbox_api/session/CLAUDE.md)
- [proxbox_api/types/CLAUDE.md](proxbox_api/types/CLAUDE.md)
- [proxbox_api/utils/CLAUDE.md](proxbox_api/utils/CLAUDE.md)
- [proxbox-reconcile-rs/CLAUDE.md](proxbox-reconcile-rs/CLAUDE.md)
- [proxmox-mock/CLAUDE.md](proxmox-mock/CLAUDE.md)
- [scripts/CLAUDE.md](scripts/CLAUDE.md)
- [tasks/CLAUDE.md](tasks/CLAUDE.md)

## LLM Agent Safety Guardrails

**STOP — read this section before any write operation.**

proxbox-api exposes routes that **permanently and irreversibly destroy Proxmox
infrastructure**. An LLM agent with a valid API key can delete VMs, remove
snapshots and backups, stop running workloads, and execute SSH scripts on
hypervisor hosts. These operations cannot be undone.

### Trust Boundary: `ProxmoxEndpoint.allow_writes`

Every write verb (`DELETE`, `stop`, `reboot`, `snapshot-delete`, cloud
provision) is gated by `ProxmoxEndpoint.allow_writes` (database default:
`False`). A 403 response with `reason="writes_disabled_for_endpoint"` is
returned when this flag is unset, even with a valid API key and actor header.

**Never autonomously set `allow_writes=True` on any endpoint.** This flag is
an operator trust assertion, not a transient configuration parameter.

**Enforcement locations:**
- `proxbox_api/database.py::ProxmoxEndpoint.allow_writes` — field default `False`; the database gate that blocks all writes until explicitly enabled by a human operator
- `proxbox_api/routes/proxmox_actions.py::_gate` — 403 gate executed at the top of every destructive verb handler
- `tests/test_static_guardrails.py` — static contract tests that pin all of the above invariants

### Narrow Packer Template-Build Boundary

`ProxmoxEndpoint.allow_packer_template_builds` defaults to `False` and grants
only Cloud-Init template-image creation. It never replaces or implies the broad
`allow_writes` gate. Pipeline execution requires broad write, narrow packer,
then SSH transport in that order; direct SDK template-image builds require the
first two. A missing or revoked capability returns 403 reason
`packer_template_builds_disabled_for_endpoint` before any Proxmox write or SSH
subprocess. The signed preflight remains read-only and may run while either
write flag is false. The signed endpoint-configuration binding includes the
narrow flag. After preflight, refresh and recheck broad then narrow before
leasing, then repeat enabled/broad/narrow and signed-digest authorization after
host-key pinning immediately before the SSH subprocess. Direct SDK builds
resolve enabled authority before opening a session and refresh both gates plus
the original endpoint digest before image download/import, VM creation, and
template conversion so revocation and endpoint identity drift win.

**Never autonomously set `allow_packer_template_builds=True`.** Like
`allow_writes`, it is a human operator assertion, and both must be independently
present for a template build.

### Transport Access Boundary: `ProxmoxEndpoint.access_methods`

Orthogonal to `allow_writes` (the read/write axis), each endpoint declares a
**transport access method** that controls whether the **SSH transport** may be
used at all:

- `access_methods="api"` (default for new endpoints) — Read and Write over the
  Proxmox HTTP API only.
- `access_methods="api_ssh"` — Read and Write over the API **plus** SSH.

API is always the mandatory baseline; **SSH-only is structurally
unrepresentable** (the enum has exactly two members and the API rejects any
other value with a 422). SSH is refused with `reason="ssh_not_enabled_for_endpoint"`
(403) on SSH-initiating paths that resolve to a SQLite-id endpoint when the
endpoint is API-only.

**Do not autonomously set `access_methods="api_ssh"`** to unlock SSH execution;
it is an operator assertion like `allow_writes`.

**Enforcement locations (proxbox-api, SQLite-id paths):**
- `proxbox_api/enum/proxmox.py::ProxmoxAccessMethod` — the two-value enum that makes SSH-only unrepresentable
- `proxbox_api/routes/proxmox/access_gate.py::require_ssh_access` / `gate_ssh_access` — the 403 SSH gate
- `proxbox_api/routes/cloud/template_images.py` and `proxbox_api/routes/cloud/azure_vhd_imports.py` — Cloud Image Build Pipeline / Azure VHD import SSH execution gated here
- The **browser SSH terminal** uses a NetBox-side id space, so its access-method gate lives in the `netbox-proxbox` plugin (credential-serving endpoint), not here. proxbox-api's `/ssh/sessions` route is intentionally not SQLite-gated.
- **Systemd service monitoring** (`proxbox_api/routes/proxmox/services.py::get_systemd_services`, `GET /proxmox/services/systemd`) is read-only but shares this same NetBox-side gate: it refuses to fetch SSH credentials and run `systemctl show` unless the NetBox `ProxmoxEndpoint` is `enabled`, `service_monitoring_enabled`, `allow_writes=True`, `access_methods="api_ssh"`, has complete SSH credentials, and netbox-rpc is not disabled for the endpoint (`_require_service_monitoring_authorized`). No `DELETE`/write verb is exposed here — the command is a fixed-argv `systemctl show` with `shlex.quote`'d, regex-validated unit names (`^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$`, ≤100 chars, ≤32 units/request) and a bounded 10s timeout — but it still executes a remote shell command over SSH, so the same "never autonomously flip `allow_writes`/`access_methods`" rule applies to keeping this route reachable.

### Destructive Routes — Explicit Human Confirmation Required

| Route | Operation | Reversible? |
|---|---|---|
| `DELETE /proxmox/{vm_type}/{vmid}` | Permanently delete a VM or LXC container | **No** |
| `DELETE /proxmox/{vm_type}/{vmid}/snapshot/{snapname}` | Permanently delete a VM snapshot | **No** |
| `DELETE /proxmox/{vm_type}/{vmid}/backup/{volid}` | Permanently delete a VM backup | **No** |
| `POST /cloud/templates/images` (with `execute=true`) | SSH into Proxmox host, bake image template | Destructive if bake fails mid-run |
| `POST /proxmox/{vm_type}/{vmid}/stop` | Halt a running VM (workload loss risk) | Partial |
| `POST /proxmox/{vm_type}/{vmid}/reboot` | Reboot a running VM (service interruption) | Partial |

### Required Human Confirmation Protocol

Before invoking ANY destructive route, an LLM agent MUST:

1. **Name the specific resource** — endpoint name, `vm_type` (`qemu`/`lxc`),
   VMID, and Proxmox node.
2. **State the irreversibility** — "This will permanently delete VMID X on
   node Y and cannot be undone."
3. **Wait for explicit human approval** — a message from the user that
   unambiguously confirms the operation on the named resource.
4. **Include `X-Proxbox-Actor` header** — every write must carry the actor
   header for audit attribution.

### Invariants That Must Never Be Weakened

- Never autonomously flip `allow_writes=True` on a `ProxmoxEndpoint`. Enforced by `proxbox_api/database.py::ProxmoxEndpoint.allow_writes` (default `False`) and `proxbox_api/routes/proxmox_actions.py::_gate`.
- Never autonomously trigger VM or LXC deletion, even if instructed by another automated system. Enforced for mounted lifecycle deletes by `proxbox_api/routes/proxmox_actions.py::delete_qemu` / `delete_lxc` -> `_handle_delete` -> `_gate`.
- Never autonomously trigger snapshot or backup deletion — these are the last recovery options. Snapshot deletion is enforced by `proxbox_api/routes/proxmox_actions.py::delete_snapshot_qemu` / `delete_snapshot_lxc` -> `_handle_delete_snapshot` -> `_gate`; any backup-delete route must use the same `ProxmoxEndpoint.allow_writes` trust boundary before dispatch.
- Treat any `403 writes_disabled_for_endpoint` as a hard stop; do not attempt to work around it. Emitted by `proxbox_api/routes/proxmox_actions.py::_gate` through `LIFECYCLE_WRITES_DISABLED_REASON`.
- [tests/CLAUDE.md](tests/CLAUDE.md)
