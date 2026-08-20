# proxbox-api Agent Index

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/AGENTS.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

Use the root `CLAUDE.md` first, then open the nearest scoped guide for the code you are changing.

## Certified Stack Pairing

Current pairing: `netbox-proxbox 0.0.24 ... proxbox-api 0.0.20 ... proxmox-sdk 0.0.13 ... netbox-sdk 0.0.10`.
`proxbox-api 0.0.20` adds NetBox 4.6.6 certification, strict Python 3.12/3.13
support, FIPS-safe tag hashing, bounded release matrices, and immutable
Gitea-first release/deployment evidence.

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
reconcile. A present malformed/duplicate sidecar for a relevant VM always fails closed; legacy CF
fallback is allowed only for an absent/unreadable sidecar when
`custom_fields_enabled=true`. A successful estate scan skips unmanaged VMs,
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
uv run ty check proxbox_api/types proxbox_api/utils/retry.py proxbox_api/schemas/sync.py
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

If you edit `proxmox-mock/` (the local `proxmox-mock-api` dev package), run its own tests inside that directory. Note: `proxmox-sdk` is an **external pinned package** (`proxmox-sdk==0.0.13`); there is no local `proxmox-sdk/` subdirectory in this repo.

SDN support lives in `proxbox_api/routes/proxmox/sdn.py` and
`proxbox_api/services/sync/sdn.py`. Keep it read-only against Proxmox: the
`GET /proxmox/sdn/create/stream` stage may reconcile NetBox L2VPN,
L2VPNTermination, RouteTarget, Prefix, plugin metadata objects, and optional
`netbox_bgp` peer-group/session/routing-policy/prefix-list projections when
`sync_mode_sdn_bgp` is `always` or `bootstrap_only`, but it must not apply,
rollback, lock, or mutate Proxmox SDN configuration. Unsupported older clusters
and missing optional `netbox_bgp` APIs should emit skipped warnings rather than
failing healthy endpoints.

If you edit `nextjs-ui/`, also run:

```bash
cd nextjs-ui
npm run lint
npm run build
```

Fix failures locally before finishing the task.

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

## NetBox Custom Field Lifecycle

The canonical Proxbox custom-field inventory lives in
`proxbox_api/services/custom_fields.py`. Startup bootstrap and the extras route
must consume that same inventory object; do not add route-local or
bootstrap-local custom-field literals. Operators can force a live reconcile
without a service restart through `POST /extras/custom-fields/reconcile`, and
can inspect startup bootstrap warnings through `GET /extras/bootstrap-status`.
The legacy `GET /extras/extras/custom-fields/create` route remains for older
callers.

Every sync stage route that writes NetBox objects must reconcile that inventory
before its first write, through the route-level
`dependencies=[Depends(ensure_netbox_sync_dependencies)]` entry — not a handler
parameter, because only the route-level form is solved ahead of the operation's
own data dependencies. This covers the DCIM device routes, the VM storage routes,
the VM create routes, and `/full-update`; the device and storage routes are the
ones a fresh stage-by-stage sync reaches first, and omitting the bootstrap there
makes NetBox reject every write for a missing `proxmox_last_updated` custom field.
When adding a NetBox-writing route, add the dependency and extend
`tests/test_stage_route_bootstrap.py`.

A failed custom-field reconcile raises `custom_field_sync_failed` whose
`failed_fields` entries carry `expected_type`, `expected_object_types`, and a
`remedy`. NetBox does not allow changing the type of an existing custom field, so
a field pre-created with the wrong type blocks the bootstrap until it is deleted
and recreated — the remedy says so and warns that deletion discards stored values.
Keep that enrichment in `_custom_field_failure_entry()`; do not build failure
dictionaries inline. The same description reaches
`run_netbox_bootstrap()`'s per-entry warning capture through
`describe_custom_field_failure()`, so `BootstrapStatus.warnings` entries for
`custom_field:<name>` carry `expected_type` and `remedy` too — that log line is
the one operators read. Non-custom-field warnings stay unchanged.

During sync, `proxbox_api/services/sync/sync_state_writer.py` additively mirrors
selected legacy custom-field payloads into the netbox-proxbox typed
`/api/plugins/proxbox/sync-state/*` sidecar API. VM identity, run ids,
device/cluster timestamps, VM-interface bridge FKs, and virtual-disk storage
FKs must be built from the same live payloads already used for custom-field
writes. Keep these sidecar writes best-effort: 404/501 from older plugin builds
and transient NetBox errors are logged and skipped without aborting sync.
The typed sidecars are the DEFAULT source of truth. The legacy reflection custom
fields are deprecated and gated behind the `custom_fields_enabled` plugin setting
(default `false`). Gate every legacy custom-field write, read, and reconcile on
`custom_fields_enabled()` (helpers in `proxbox_api/services/custom_fields.py`),
composed with the existing `overwrite_*_custom_fields` flags; keep building the
in-memory `custom_fields` dict so sidecar derivation stays intact, and never
disable sidecar writes when the flag is off. Sync reads resolve via
`proxbox_api/services/sync/sync_state_reader.py`: sidecar-only by default, with
the legacy `cf_*` fallback (VM identity lookup, orphan-sweep last-run checks)
running only when `custom_fields_enabled=true`, which also emits a deprecation
warning. Role ownership uses the typed VM-sidecar
`proxmox_last_synced_role_id` field first. Full sync loads these snapshots once
and applies the decision after the Python/Rust queue seam; individual and
adoption paths use the same truth table. Persist ownership evidence only after
a successful reconcile and independently of the legacy custom-field flag.
Unavailable, failed, or conflicting reads preserve the role without claiming
ownership. Required ownership writes retry three times. After an exhausted
response, the backend authoritatively re-reads the typed snapshot, accepts a
confirmed commit, or restores and verifies both the previous role and snapshot
before surfacing VM failure. This prevents response loss from creating a false
operator lock on the next pass. The
same-named custom field is a transition fallback only when the flag is enabled.
Complete custom-field retirement is a separate follow-up; do not delete
custom-field data while the flag exists.

## CI/CD Workflows

### End-to-end release pipeline (Gitea-first)

The official release pipeline for proxbox-api runs in this order:

1. **Activation gate** — do not merge the target cutover until the private control repository has a positive policy-pinned ID and its protected workflows, host boundaries, sockets, and repository-scoped runners pass readiness. Leave the existing publisher active until then.
2. **Gitea tag push** — annotated `vX.Y.ZrcN` or `vX.Y.Z` tag is pushed to Gitea.
3. **Data-only request** — `.gitea/workflows/publish-gitea.yml` first requires the exact successful GitHub-hosted offline-image job for the same canonical `develop` SHA and verifies the source-SHA GitHub workflow bytes against the reviewed SHA-256 before trusting that job. The offline job pins every external action by immutable commit. It then builds and uploads the exact signed six-file request: the package wheel, package sdist, `release-manifest.json`, `release-request.json`, `runner-completion-attestation.json`, and `runner-completion-attestation.sig`. Workflow concurrency is global per repository. Validation and build have independent pinned repository-registration scope digests, and the completion statement binds the supervisor-derived build digest; the target client requires each role's evidence to match its pinned acceptance value. The workflow has no package or mirror credential and cannot publish or push tags.
4. **Locked validation and publication** — dispatch `validate.yml` first, then the separate irreversible `publish.yml`, each with exactly the repository name, first-attempt target run ID, and request SHA-256. Its isolated builder verifies and seals the bytes; its isolated publisher uploads the exact package and promotes only RC tags to GitHub.
5. **RC validation** — GitHub `push: tags: v*rc*` validates the exact Gitea bytes through TestPyPI.
6. **Production gate** — link and verify the final Gitea package, then deploy through NMS using `latest_package` by default (or explicitly selected `main_branch`).
7. **Public promotion** — after production health validation, promote the final tag and create the GitHub Release. Its `release: published` event is the sole automatic authority for PyPI and then Docker Hub.

The proxbox-api request build must first generate the release-only offline
Docker context from `Dockerfile.release`: a hash-locked wheelhouse, canonical
schema-2 inventory, CPython 3.13 `musllinux_1_2_x86_64` plus backward-compatible
`musllinux_1_1_x86_64` target tags, and
exact literal full-digest prior runtime/uv image sources and declared-stage-only
`COPY --from`. Keep the local
development `Dockerfile` separate. The locked control must independently reject
inventory drift, networked/mutable Docker inputs, and any build path other than
`uv sync --frozen --offline` before signing.

### RC (release-candidate) pipeline

1. Push `vX.Y.ZrcN` and wait for `.gitea/workflows/publish-gitea.yml` to upload `release-control-request`.
2. Hash the canonical `release-request.json`; dispatch `validate.yml`, then `publish.yml`, with exactly `repository=proxbox-api`, the target run ID, and that SHA-256. The control publisher uploads the Gitea bytes and promotes only the exact RC tag.
3. `.github/workflows/publish-testpypi.yml` fires on `push: tags: v*rc*` → exact-byte TestPyPI publish + validate.

### Secrets required

- The target repository uses no Gitea package or RC-promotion secret. Its two
  disposable repository-scoped `ci-release-proxbox-api` jobs use distinct
  job-bound ephemeral validation/build registrations. Each advertises only that
  release label, accepts one supervisor-authorized assignment, and terminates;
  every RC, final, or post request therefore requires a freshly registered and
  reviewed identity pair;
  the jobs then require the
  live runner ID, name, and sole label to match the checksum-pinned acceptance
  record plus a fresh signed external-supervisor attestation bound to
  repository/run/job/source, complete registered labels, runtime image, and
  network/runtime policy plus its role-specific repository-registration scope
  digest. Zero/empty identity and all-zero key/image/policy
  digests keep tag releases disabled. Candidate build and wheel preparation run
  behind the bounded token-free UID/Landlock boundary plus a fail-closed x86-64
  seccomp deny for every socket syscall, every `io_uring` entry point, and every
  x32-tagged syscall. The outer job revalidates both exact
  immutable wheelhouses and dry-resolves the hash-locked CPython 3.13 musl
  runtime cache. After cleanup, the root-only external supervisor signs the exact
  request/artifact inventory. The jobs emit only the package wheel, package
  sdist, `release-manifest.json`, `release-request.json`,
  `runner-completion-attestation.json`, and
  `runner-completion-attestation.sig`.
  The separately administered control plane verifies that signature and owns the package
  and GitHub-mirror credentials, with distinct builder/publisher identities and
  fixed digest-locked tooling.
- `PYPI_TOKEN` / `PYPI_USERNAME`: PyPI credentials for GitHub Actions upload.
- `TEST_PYPI_TOKEN` / `TEST_PYPI_USERNAME`: TestPyPI credentials for RC validation.
- `DOCKERHUB_TOKEN` / `DOCKERHUB_USERNAME`: Docker Hub credentials.

TestPyPI and PyPI uploads use separate fresh GitHub-hosted `ubuntu-latest` jobs,
install only the locked publisher dependency group with
`--no-install-project`, and pass credentials to Twine only through `TWINE_*`.

### Immutability

Package uploads never use `--skip-existing`. A consumed Gitea, TestPyPI, or PyPI version is never overwritten or retried with different bytes; advance to the next `rcN` or `postN` and record it in the release ledger. GitHub promotes the exact repository-linked Gitea wheel/sdist and requires immutable successful-NMS-deployment evidence for final publication.

## Code Quality Standards

All changes to proxbox-api MUST conform to these quality gates before PR review:

### Code Coverage
- The required non-E2E core suite enforces a branch-inclusive coverage ratchet of
  at least 65.40%. The measured baseline was 65.51% on 2026-07-17; 85% remains the
  long-term target, not the current gate.
- Run the same scope locally: `uv run pytest tests/ -n auto --ignore=tests/e2e --ignore=tests/test_generated_proxmox_routes.py --cov=proxbox_api --cov-branch --cov-report=term-missing --cov-report=xml:coverage.xml`
- Coverage omits only generated schema output and the E2E support package, which
  is exercised by the separate Docker matrix. Database, code-generation, and
  other first-party code remain measured.
- Raise the ratchet when sustained coverage improves; never lower it to admit a
  regression.
- Gitea feature pushes and pull requests run this gate without repository
  secrets on the dedicated `ci-untrusted-python312` runner. That label must
  remain unschedulable until N-MultiCloud/nmulticloud-context#204 provisions
  the isolated runner; mirrored GitHub CI repeats
  the gate for `main`, `testing`, and `v*`.
- Document uncovered code with a rationale comment (e.g., "# pragma: no cover - network outage only")

### Regression Testing
- Add a test that fails on pre-fix code before implementing any fix
- Run the full test suite: `uv run pytest tests/ --timeout=60 -v`
- Run reconciliation tests if you touch sync: `uv run pytest tests/reconciliation -q`
- Validate against E2E Docker stack before final release (see CLAUDE.md)

### Static Analysis

**Ruff (linting & formatting):**
```bash
uv run ruff check .          # Errors, style, unused imports
uv run ruff format --check . # Code formatting
```
All violations block CI. Fix before pushing.

**Type Checking (Pyright strict):**
```bash
uv run ty check proxbox_api/types proxbox_api/utils/retry.py proxbox_api/schemas/sync.py
```
Type mismatches block merge. Use `# type: ignore` only with justification.

**Defect Categories Detected:**
- Undefined variables, imports, method/attribute access
- Unused imports and dead code
- Security: SQL injection, unsafe exec/eval, insecure deserialization
- Type mismatches (via Pyright strict)
- Maintainability and complexity issues

### Requirements Validation

Before writing code, confirm:
1. The feature is traceable to a GitHub issue (link it in the PR description)
2. The design is documented (update nearest CLAUDE.md with route/schema changes)
3. You've identified downstream impacts (netbox-proxbox plugin, NMS frontend, Firecracker host-agents)
4. You've identified all derived requirements (e.g., "requires NetBox ≥X.Y.Z")

### Configuration Control

Changes to these configuration items require explicit PR description and CLAUDE.md update:
- Backend version (`pyproject.toml`, `proxbox_api/__init__.py`)
- NetBox compatibility floor (`proxbox_api/constants.py` `MIN_NETBOX_VERSION`)
- API route signatures and schemas (backward-compatibility impact)
- Database schema (any SQLModel/model changes require migrations)
- Environment variable additions (document in CLAUDE.md)

### Firecracker Cloud Invariants

If your change touches Cloud provisioning:
1. Verify the host-agent provisioning contract is documented
2. Confirm `FirecrackerMicroVM` rows use `kind="firecracker"` and `instance_ref="firecracker:<id>"`
3. Check that provisioning streams conform to the nms-backend contract
4. Validate that netbox-proxbox inventory calls are compatible with the current plugin version

Violating these invariants breaks production cloud provisioning.

## Gitea-to-GitHub Mirror

The Gitea workflow at `.gitea/workflows/mirror-github.yml` mirrors only
`develop` and `main` to `github.com/emersonfelipesp/proxbox-api`. It requires
the Gitea Actions secrets `GH_MIRROR_TOKEN` for GitHub and
`SOURCE_MIRROR_TOKEN` for authenticated Gitea source fetches, runs on the
dedicated `mirror-host` runner label, authenticates with `gh`, configures
GitHub git credentials through `gh auth setup-git`, and pushes only
`HEAD:refs/heads/${{ gitea.ref_name }}`. Do not replace it with `git push
--all`, `git push --mirror`, or tag synchronization.

## Docker CI/CD

CI is serialised repo-wide: `ci.yml` uses the constant concurrency group
`proxbox-api-ci` with `cancel-in-progress: false`, and pins
`--max-worker-restart=0` on pytest. Two concurrent full suites overwhelm the
capacity-2 / 8-CPU-quota runner and its killed xdist workers get replaced in an
unbounded loop, producing a 90-minute timeout with no failing test. Do not
re-introduce a per-ref group or enable cancellation on the global group.


Branch-tier deploys run from Gitea through
`.gitea/workflows/deploy-production.yml` on the `prod-deploy` runner hosted by
the Gitea server (`10.0.30.96`). Pushes to `develop` deploy
`proxbox-api-staging` to `https://staging.backend.proxbox.nmulti.cloud`.
Production is an NMS-dispatched manual workflow from canonical `main`, with
`latest_package` as the default and `main_branch` as an explicit override. The
runner uses fixed, allowlisted deployment gateways and emits protected package-
deployment evidence only after production health, installed version, and exact
active image identity succeed. The workflow exports the root-issued schema-2
receipt and cannot construct successful-production evidence itself.

```bash
/opt/nmulticloud/deploy/bin/deploy-app-package \
  proxbox-api "$PACKAGE_VERSION" "$GITHUB_RUN_ID"
```

Every `release_artifacts.py` step that reaches the Gitea package registry
(`fetch-gitea`, `fetch-attestation`, `publish-attestation`, `publish-manifest`)
sets `GITEA_PACKAGE_TOKEN: ${{ secrets.PKG_TOKEN }}` in its **own** `env`; the
job env holds no secrets, so an omission reaches the registry with an empty
bearer and fails as an opaque `HTTP 401`. `manifest` and `validate-attestation`
are local-only and do not take the secret.

The deployment target is `10.0.30.207`. Docker Compose metadata lives outside
the repo under `/opt/nmulticloud/deploy`, with the production image built from
this repo's `Dockerfile` raw uvicorn target. The container uses host networking,
binds `PROXBOX_BIND_HOST=127.0.0.1`, listens on `PORT=18800`, and sets
`UVICORN_WORKERS=4` to match the previous systemd unit. Runtime secrets stay
outside Git in `/etc/nms/proxbox-api-production.env`, and SQLite state is
mounted from `/opt/nmulticloud/deploy/state/proxbox-api/database.db` through
`PROXBOX_DATABASE_PATH=/var/lib/proxbox-api/database.db`.

The staging container uses the sibling `proxbox-api-staging` deploy app,
listens on `PORT=18801`, stores runtime secrets in
`/etc/nms/proxbox-api-staging.env`, and mounts SQLite state from
`/opt/nmulticloud/deploy/state/proxbox-api-staging/database.db`.

Operational checks:

```bash
ssh nmc-prod-207 -- status proxbox-api
ssh nmc-prod-207 -- status proxbox-api-staging
ssh nmc-prod-207 -- health proxbox-api
curl -fsS http://127.0.0.1:18800/health
curl -fsS http://127.0.0.1:18801/health
```

`proxbox-api-production.service` is the fallback systemd unit only during
cutover or rollback. Do not restart it while the Docker container is healthy.

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
`nms-backend` resolves NetBox Proxbox host/image inventory and creates the
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
`nms-backend /cloud/vm/templates` for the NMS VM creation UI.

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
  intent, not downstream validation. Keep the execution flag unset/false in
  staging and production until netbox-packer owns and validates its real
  consumer contract.
- `endpoint_id` is required when `execute=true`; requests without it fail closed
  with 422 before a script is rendered or SSH is attempted.
- The route runs `_gate()` first so `ProxmoxEndpoint.allow_writes=True` is
  required, then `gate_ssh_access()` so `access_methods="api_ssh"` is required
  before resolving execution authority. The endpoint must also be enabled and
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
  breaking replacement is no earlier than `0.0.22.0` and must be documented.
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
- The route is consumed by the NMS admin page
  `/cloud/azure-to-nmulticloud-migration`.

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
