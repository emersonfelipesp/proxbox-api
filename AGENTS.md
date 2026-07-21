# proxbox-api Agent Index

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/AGENTS.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

Use the root `CLAUDE.md` first, then open the nearest scoped guide for the code you are changing.

## Certified Stack Pairing

Current pairing: `netbox-proxbox 0.0.22 ... proxbox-api 0.0.19.post5 ... proxmox-sdk 0.0.13 ... netbox-sdk 0.0.10`.
`proxbox-api 0.0.19` ships the Proxmox SDN sync collectors, NetBox
L2VPN/RouteTarget/Prefix reconcile, plugin inventory reconciliation, and
VM-interface reconcile idempotency hardening.

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

## NetBox Custom Field Lifecycle

The canonical Proxbox custom-field inventory lives in
`proxbox_api/services/custom_fields.py`. Startup bootstrap and the extras route
must consume that same inventory object; do not add route-local or
bootstrap-local custom-field literals. Operators can force a live reconcile
without a service restart through `POST /extras/custom-fields/reconcile`, and
can inspect startup bootstrap warnings through `GET /extras/bootstrap-status`.
The legacy `GET /extras/extras/custom-fields/create` route remains for older
callers.

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
warning. Role-ownership snapshots have no sidecar field and are read only when
the flag is enabled. Complete custom-field retirement is a separate follow-up; do
not delete custom-field data while the flag exists.

## CI/CD Workflows

### End-to-end release pipeline (Gitea-first)

The official release pipeline for proxbox-api runs in this order:

1. **Gitea tag push** — annotated tag `vX.Y.Z` pushed to Gitea (`git push gitea vX.Y.Z`).
2. **Gitea Actions: `.gitea/workflows/publish-gitea.yml`** — builds dist, publishes to Gitea Package Registry (`PKG_TOKEN`), pushes tag to GitHub, creates or publishes GitHub release (`GH_MIRROR_TOKEN`).
3. **GitHub Actions: `push: tags: v*` trigger** — fires when Gitea workflow pushes tag to GitHub. Validates version, runs validate and E2E checks, then publishes to PyPI.
4. **GitHub Actions: `release: published` trigger** — fires when Gitea workflow creates the GitHub release. The `publish-pypi` job has a pre-check: if the version already exists on PyPI (from the tag-push run), the upload is skipped and the run succeeds.
5. **Docker Hub** — `publish-docker` job in `publish-testpypi.yml` calls `docker-hub-publish.yml` after PyPI validation.

### RC (release-candidate) pipeline

1. Push `vX.Y.ZrcN` tag → `.gitea/workflows/publish-gitea.yml` publishes to Gitea registry and pushes tag to GitHub.
2. `.github/workflows/publish-testpypi.yml` fires on `push: tags: v*rc*` → TestPyPI publish + validate.

### Secrets required

- `PKG_TOKEN`: Gitea Personal Access Token with `write:packages` scope. Name must be exactly `PKG_TOKEN` — `GITEA_` prefix is reserved by Gitea Actions.
- `GH_MIRROR_TOKEN`: GitHub PAT with `repo` and `workflow` scopes for tag push and release creation.
- `PYPI_TOKEN` / `PYPI_USERNAME`: PyPI credentials for GitHub Actions upload.
- `TEST_PYPI_TOKEN` / `TEST_PYPI_USERNAME`: TestPyPI credentials for RC validation.
- `DOCKERHUB_TOKEN` / `DOCKERHUB_USERNAME`: Docker Hub credentials.

### Idempotency

The `publish-pypi` job checks the PyPI API before uploading. If `proxbox-api==${VERSION}` already exists (HTTP 200), the upload step is skipped and the job succeeds. This prevents failures when `release: published` re-triggers after the tag-push run already published.

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

Branch-tier deploys run from Gitea through
`.gitea/workflows/deploy-production.yml` on the `prod-deploy` runner hosted by
the Gitea server (`10.0.30.96`). Pushes to `develop` deploy
`proxbox-api-staging` to `https://staging.backend.proxbox.nmulti.cloud`.
Pushes to `main` deploy `proxbox-api` to
`https://backend.proxbox.nmulti.cloud`. The workflow uses the restricted SSH
alias `nmc-prod-207` and the allowlisted command:

```bash
ssh nmc-prod-207 -- deploy <proxbox-api|proxbox-api-staging> "$GITHUB_SHA"
```

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
`PROXBOX_BIND_HOST`, `PROXBOX_DATABASE_PATH`, `PROXBOX_RATE_LIMIT`,
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

Execution rules:

- `PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION=true` is mandatory for remote execution.
- `endpoint_id` is required when `execute=true`; requests without it fail closed
  with 422 before a script is rendered or SSH is attempted.
- The route runs `_gate()` first so `ProxmoxEndpoint.allow_writes=True` is
  required, then `gate_ssh_access()` so `access_methods="api_ssh"` is required
  before the pipeline can start `ssh ... bash -s`.

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
