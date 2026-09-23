# proxbox-api Agent Index


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

This file lives at `<repository-root>/AGENTS.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

Use the root `CLAUDE.md` first, then open the nearest scoped guide for the code you are changing.

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

Generated `/proxmox/api2/*` proxy dispatch is read-only. Every non-GET method is refused before target and credential resolution, including cached and rebuilt routes. Mutation schemas remain discoverable but deprecated with a documented 403; use dedicated typed, audited RPC procedures for supported writes. This method guard does not establish effect safety for all GET operations or change handwritten route authorization.

## Proxmox Code Generation Security

Runtime code generation defaults to disabled. Only the explicit development
setting `PROXBOX_RUNTIME_CODEGEN_ENABLED=true` mounts the HTTP generation and
route-refresh endpoints or permits runtime user-schema discovery. With the
default setting, route registration, schema discovery, and Pydantic source
rendering use bundled schemas only; the user-generated directory may receive
only the derived route cache. Refresh a bundled tag, including `latest`, solely
by installing a replacement package that bundles the updated schema.

Runtime-generated routes construct Pydantic models directly from parsed OpenAPI
data with `pydantic.create_model`; runtime startup and route refresh never
evaluate rendered Python source. The file renderer remains available for
offline artifacts, but it must validate every emitted identifier and render
aliases, descriptions, and defaults with Python literal representations.
Codegen version tags must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` and must
not equal `.` or `..`. Validate tags before any crawl or write, and resolve
every generated artifact and runtime route-cache path inside its configured
base directory before accessing the filesystem.
Bundled tags are immutable and resolve before user-generated tags. Register a
user artifact only when its `provenance.json` sidecar contains the source URL,
generation time, and the matching SHA-256 digest of `openapi.json`. Persist
non-default-source artifacts under `custom/<version_tag>/` for inspection only;
runtime registration must never read that namespace. Enforce all OpenAPI
resource limits before persistence, cache restoration, and model construction,
and reject normalized field-name or operation-derived model-name collisions.
Startup and `proxbox-schema quarantine-legacy` quarantine every user-generated
`pydantic_models.py` and each legacy runtime route cache without valid
provenance.
Treat provenance sidecars as corruption detection, not authentication: a writer
with the same operating-system UID can forge both an artifact and its digest.
Production must therefore keep runtime code generation disabled.

## Certified Stack Pairing

Current sibling-source development pairing: `netbox-proxbox 0.0.27rc4 ... proxbox-api 0.0.23 ... proxmox-sdk 0.0.15 ... netbox-sdk 0.0.13`.
This source-only tuple is not a published or certified runtime pairing.
`proxbox-api 0.0.21.post2` adds authenticated Proxmox console WebSocket handshakes,
typed-sidecar-only inventory state, NetBox 4.6.6 certification, strict Python
3.12/3.13 support, and a verified network-free production release context.

## Gitea Package Publication

`.gitea/workflows/publish-gitea.yml` is a package-only control. Dispatch it
only from canonical `main` with an exact immutable tag. It must not mirror tags,
create GitHub releases, deploy services, or contact runtime environments.
Registry artifacts are verified against a canonical source-bound manifest
before the repository-linked manifest is published. Existing versions require
explicit `resume_existing=true` and exact byte equality; otherwise use a new
fixed-forward version.

## VM Interface Sync Strategy

VM sync routes accept `vm_interface_sync_strategy`. The default
`guest_os_model` keeps the core NetBox `virtualization.VMInterface` named by
Proxmox config (`net0`, `net1`, ...) and writes guest OS interface rows
(`ens18`, `eth0`, ...) through netbox-proxbox plugin endpoints. Guest address
rows must reference the already-reconciled core `ipam.IPAddress` IDs; never
create duplicate IPAM records for the guest side. If those plugin endpoints are
missing on an older netbox-proxbox release, log and skip guest writes without
failing core interface/IP sync.

`legacy_rename` is deprecated compatibility mode. It preserves the previous
`use_guest_agent_interface_name=true` behavior that renames the core
VMInterface to the guest OS name and must emit a deprecation warning.

## Task History Sync Ownership

VM create routes default `sync_task_history=true` for backward compatibility
and run one aggregate after successful VM IDs are known. Full-update passes
`false` to the VM stage and owns one dedicated task-history stage. Roll out the
backend first before an orchestrating plugin begins sending `false`.

Bulk task history is node-oriented: paginate each selected node archive with a
fixed run-start `until`, load the typed VM sync-state sidecar once, map by its
endpoint + cluster + VMID identity, deduplicate UPIDs, then issue one NetBox bulk
reconcile. A present malformed/duplicate sidecar for a relevant VM always fails closed. There is no
custom-field fallback. A successful estate scan skips unmanaged VMs,
but explicitly selected VMs without identity remain fatal. Encode selected
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

## Required Checks

Run these before pushing anything that touches the backend package:

```bash
rtk ruff check .
rtk ruff format --check .
uv run python -m compileall proxbox_api tests
uv run python -c "import proxbox_api.main"
uv run python -c "from proxbox_api.proxmox_to_netbox.proxmox_schema import load_proxmox_generated_openapi; assert load_proxmox_generated_openapi().get('paths')"
uv run ty check proxbox_api/types proxbox_api/utils/retry.py proxbox_api/schemas/sync.py \
  proxbox_api/database_protocols.py proxbox_api/utils/async_compat.py \
  proxbox_api/runtime_settings.py proxbox_api/settings_client.py \
  proxbox_api/ceph/endpoint_binding.py proxbox_api/ceph/timing.py \
  proxbox_api/ceph/v2_schemas.py \
  proxbox_api/ceph/v2_engine.py proxbox_api/ceph/v2_routes.py \
  proxbox_api/ceph/v2_providers/base.py proxbox_api/ceph/v2_providers/proxmox.py \
  proxbox_api/ceph/v2_providers/proxmox_writer.py
rtk pytest tests
```

If you edit VM reconciliation or the Rust bridge (`proxbox_api/services/sync/reconciliation/`,
`tests/reconciliation/`, `benchmarks/reconciliation/`, `proxbox-reconcile-rs/`,
or `.github/workflows/rust-reconcile.yml`), also run:

```bash
cargo test --no-default-features --manifest-path proxbox-reconcile-rs/Cargo.toml
uv pip install -e proxbox-reconcile-rs
PROXBOX_RECONCILIATION_ENGINE=compare \
  PROXBOX_RECONCILIATION_COMPARE_STRICT=true \
  uv run pytest tests/reconciliation -q
```

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

If you edit `nextjs-ui/`, also run:

```bash
cd nextjs-ui
npm run lint
npm run build
```

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
run from this repository: `.github/workflows/` publication, the bounded untrusted
workflow under `.gitea/workflows/`, package/runtime configuration, and public API
behavior. Do not reintroduce private runner inventories, deployment receipts,
package-registry credentials, internal promotion topology, or removed release
orchestration scripts into README, MkDocs, or LLM guidance.

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

**Prefer DB-backed plugin settings over `.env` variables.**
When adding a new runtime tunable, default to making it a `ProxboxPluginSettings` field
(NetBox-UI-editable, persisted in the NetBox database) and read it via
`proxbox_api.runtime_settings.get_int / get_float / get_bool / get_str`, which already
resolves **env var (override) → `ProxboxPluginSettings` → built-in default** with a
5-minute settings cache (`proxbox_api/settings_client.py::get_settings`).

Only fall back to a pure `.env` variable when the value is needed **before** the NetBox
connection exists or is **operator-only infrastructure** that has no business in the UI:
`PROXBOX_BIND_HOST`, `PROXBOX_DATABASE_PATH`, SQLite `DATABASE_URL`, `PROXBOX_RATE_LIMIT`,
`PROXBOX_ENCRYPTION_KEY` / `PROXBOX_ENCRYPTION_KEY_FILE`, `PROXBOX_STRICT_STARTUP`,
`PROXBOX_SKIP_NETBOX_BOOTSTRAP`, `PROXBOX_GENERATED_DIR`,
`PROXBOX_RUNTIME_CODEGEN_ENABLED`,
`PROXBOX_CORS_EXTRA_ORIGINS`. Anything that controls sync behavior, batching,
concurrency, caching, or feature toggles belongs in `ProxboxPluginSettings`.

Do **not** invent shadow config layers (parallel JSON/YAML files, ad-hoc dotenv
sections, module-level constants meant as overrides) to dodge the migration cost.
If the new field needs the model + migration + form + serializer + template wiring on
the `netbox-proxbox` side, do all five — the existing fields in
`netbox-proxbox/netbox_proxbox/models/plugin_settings.py` and migration
`0037_pluginsettings_runtime_tunables.py` show the pattern.

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

Read the nearest scoped guide for the code you are changing.

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
