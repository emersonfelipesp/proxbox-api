# proxbox_api/ceph Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/ceph/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.

---

## Purpose

Ceph integration surface for `proxbox-api`. Two layers:

- **v1 (`/ceph`)** — read-only reflected inventory of Proxmox-managed Ceph
  (`routes.py`, `schemas.py`, `client.py`, `inventory.py`). Status + sync
  endpoints; every sync route accepts `netbox_branch_schema_id`.
- **v2 (`/ceph/v2`)** — the NetBox-driven control plane (issue #95). Lets NetBox
  fully configure Ceph desired-state without direct operator access to Proxmox
  or external Ceph tooling.

Both routers are mounted in `proxbox_api/app/factory.py` (`/ceph` and `/ceph/v2`).

## v1 Files

| File | Role |
|------|------|
| `routes.py` | FastAPI router for `/ceph/status` and `/ceph/sync/{status,daemons,osds,pools,filesystems,crush,flags,rgw,rbd,full}`. RGW/RBD routes preserve the `CephSyncResponse` summary envelope and include reflected inventory in `raw`. |
| `client.py` | Internal read-only Proxmox VE Ceph facade used when the installed `proxmox-sdk` lacks or changes the read namespace. |
| `inventory.py` | RGW/RBD v1 inventory normalization. Uses optional client/provider helpers when present and the same PVE Ceph pool reads as the existing sync routes for RGW/RBD pool discovery. Redacts RGW credential fields before returning raw inventory. |

## v2 Files

| File | Role |
|------|------|
| `v2_schemas.py` | Pydantic contract: desired state, exact operation-node binding, endpoint-bound plan/approval/apply, safe approval status, ordered operation events, run, provider, validation, metric, and SSE schemas. It recursively normalizes/redacts secret aliases. Legacy apply fields remain parseable only for stable rejection; they are not write authority. |
| `v2_engine.py` | Durable plan/approval/apply engine: canonical digests, expiring hashed approvals, atomic consumption, owner-bound live leases, append-only pre-dispatch/task checkpoints, permanent provider-global atomic task claims, repeated-cancellation-safe task/synchronous/cancellation evidence, unique node-consistent UPID handling, replay recovery, recursive secret/fallback redaction, and read-only reconciliation. There is no process-local plan authority. |
| `endpoint_binding.py` | Resolves exactly one local DB endpoint, creates exactly one request-private session, persists only a stable server-keyed revision of the complete mutation-relevant endpoint configuration, and binds endpoint/session connection/auth/TLS/timeout/retry schemas with a second per-request secret HMAC. No endpoint secret or ephemeral key/tag is persisted or exposed. |
| `v2_providers/base.py` | Adapter contract plus fail-closed capability, provider-boundary and write-gate errors and terminal-task polling hook. |
| `v2_providers/proxmox.py` | Reads/diffs/plans and applies through the privately bound endpoint session. Every mutation keeps one exact node outside the SDK payload, reloads `enabled`/`allow_writes`, and constant-time compares both HMAC-bound schemas immediately before dispatch. The endpoint gate and run heartbeat serialize database access; UPIDs are polled on the same node/session. Never restore `_pxs[0]`, first-node, or `localhost` mutation fallback. |
| `v2_providers/proxmox_writer.py` | Explicit-default-deny `(kind, action) -> CephWrite` table plus strict Pydantic payload schemas used at planning and dispatch. Supports pool, flag, OSD, MON/MGR/MDS and CephFS operations listed in the map; unsupported pairs, unknown keys, and missing required fields are blocked rather than filtered. A returned UPID is `submitted`, never proof of completion. Only SDK-proven flag create/update/delete and OSD update return explicit typed synchronous completion after a successful `None`. |
| `v2_providers/__init__.py` | Adapter registry: `adapter_for_provider(provider, pxs)`, `provider_names()`. Stub adapters for `dashboard` (#98), `rgw_admin` (#12), `rbd` (#12), `prometheus` (#94), `external` (#97). |
| `v2_routes.py` | FastAPI router for all `/ceph/v2/*` endpoints (thin; delegates to the engine). |

## Endpoint Map (`/ceph/v2`)

| Method + Path | Purpose |
|---|---|
| `GET /capabilities` | Endpoint-scoped provider flags; unscoped Proxmox apply is false |
| `POST /validate` | Validate a desired object or full bundle |
| `POST /plans`, `GET /plans/{id}` | Build/persist and inspect a canonical endpoint-bound plan |
| `POST /plans/{id}/approvals` | Issue one short-lived opaque approval to a distinct actor |
| `GET /approvals/{id}` | Safe approval/recovery metadata only; never raw token/hash |
| `POST /plans/{id}/apply` | Atomically consume approval and apply only the persisted plan |
| `POST /plan`, `POST /apply` | Flat aliases; `/apply` still requires a persisted plan/token |
| `GET /operations/{id}` | Operation run status, task refs, warnings, errors, result |
| `GET /operations/{id}/events` | SSE operation-progress stream (`text/event-stream`) |
| `POST /reconcile` | Reconcile provider state back into NetBox summaries |
| `GET /metrics` | Latest provider metrics for a scope |

Persistence: `ceph_plan`, `ceph_approval`, `ceph_operation_run`, and the
append-only `ceph_operation_event` ledger in `proxbox_api/database.py`. Startup
creates new tables and additively migrates legacy run fields without changing
endpoint `allow_writes` values.

## Rules

- **Secrets never reach NetBox.** Providers resolve real credentials behind the
  adapter layer; the engine runs recursive `redact_secrets()` on persistence,
  API, and SSE payloads. Normalize snake/camel/kebab/space aliases so fields
  such as `access_key`, `rgw_access_key`, `accessKey`, `apiKey`, and
  `privateKey` are always redacted. Logger handlers must sanitize deferred
  arguments and exception objects too. NetBox passes only opaque
  `credential_ref` pointers.
- **Every plan/approval/apply names one durable endpoint.** Resolve exactly one
  local DB row and create exactly one private session from it. Generic query
  selectors, generic session dependencies, colliding IDs, bare fake sessions,
  missing/disabled/duplicate rows and schema drift fail closed.
- **Write execution is default-off.** Capability/approval/apply and provider
  sinks require both `PROXBOX_ENABLE_CEPH_V2_WRITES=true` and
  `PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY=true`. The gateway must authenticate and
  overwrite `X-Proxbox-Actor`; a flag is only the operator attestation.
- **Bind the stable endpoint revision through every authority record.** Persist
  it with plan, approval, and run; legacy missing revisions and same-ID
  retargeting fail closed. Keep the per-request endpoint/session HMAC as the
  adjacent dispatch check.
- **Every provider mutation reloads write authority.** Check the persisted
  endpoint's `enabled` and `allow_writes` and compare the endpoint/session HMAC
  immediately before every non-noop SDK call. Planning, metrics, and reconcile
  remain read-only.
- **Every Proxmox mutation has an immutable node and typed payload.** Keep node
  outside SDK arguments in `ProviderOperation.node`, preserve it for deletes,
  verify it belongs to the selected endpoint, and reject ambiguity. Validate
  the exact per-kind/action schema at planning and sink; never silently drop
  unsupported keys.
- **Task UPID means submitted.** Persist intent before dispatch, require the complete
  Proxmox UPID structure, atomically claim exactly one globally new reference
  whose returned and embedded nodes match the plan node, then persist submission and poll the exact
  node/session. Only stopped/OK is completed; failure is failed, and
  crash/transport/timeout/cancellation is `outcome_unknown` and must not be
  blindly retried. The pre-SDK state stays nonterminal `dispatching` while the
  worker heartbeats a renewable lease. Every later live checkpoint CASes the
  unexposed lease owner and expiry; a stale status/SSE read atomically records
  `run_lease_expired`, clears ownership, preserves task references for operator
  recovery, and prevents a late worker from appending or overwriting recovery.
  The claim/submission transaction and SDK-proven synchronous completion
  checkpoints repeatedly re-enter cancellation shielding until the inner
  durability task finishes, then re-raise the remembered cancellation before
  conservative recovery returns control.
  Endpoint freshness queries and heartbeat renewal share one database lock.
- **No legacy confirmation authority.** Boolean/predictable confirmation and
  inline apply remain parseable only for explicit rejection. Only the hashed,
  expiring, single-use, two-person approval bound to the canonical plan,
  endpoint, requester, and approver authorizes apply.
- **No silent capability gaps.** Unsupported operations are surfaced as blocked
  with a reason. Dashboard/external advertise false apply, destructive, and
  mutation-kind capabilities and their sinks reject mutations until each has a
  durable selector, revision, credential authority, and fresh mutation gate.
- **Thread the branch id.** Pass `netbox_branch_schema_id` through
  plan/apply/reconcile so branch-aware NetBox staging stays consistent.
- v2 is additive; do not change v1 `/ceph` behavior.
- The Proxmox adapter now executes PVE Ceph writes through `proxmox_writer` +
  proxmox-sdk `CephWrite` (#12 part 1 / #224). The current `proxmox-sdk`
  pin (`0.0.13`) ships the `CephWrite` domain, so the adapter advertises
    the SDK write surface, but live execution remains default-off behind the two
    Ceph safety flags. An older pin without `CephWrite` degrades cleanly via
    `cephwrite_importable()` to `apply=False` and blocks writes with a clear
    reason rather than silently no-op'ing.
  Dashboard/RGW/RBD/Prometheus/external adapters arrive with #98/#12/#94/#97.

## Tests

`tests/ceph/test_v2_orchestration.py` — HTTP contract and adversarial security
coverage for exact private endpoint/session/node binding and drift,
`allow_writes`, strict typed payload rejection, canonical persistence, actor
binding, token races/replay, recursive persistence/API/SSE secret canaries,
unique node-consistent UPIDs, approval recovery, terminal task states, ordered
events and read-only reconcile.

`tests/ceph/test_v2_approval_concurrency.py` — concurrent single-use approval
consumption plus live dispatching/lease-owner CAS, sequential/concurrent durable
task-claim uniqueness, shielded task/synchronous cancellation checkpoints,
heartbeat/session serialization, crash/cancellation ambiguity,
expiry recovery, and stale-worker nonterminalization checkpoints. The exact AsyncSession/gather path runs on the CI Python 3.12
toolchain; it is narrowly skipped on local Python 3.14 where the aiosqlite
connection worker does not complete. A two-connection SQLite race remains live
locally and proves one winner, one run, and one provider call.

`tests/ceph/test_v2_proxmox_writer.py` — the `(kind, action) -> CephWrite`
mapping, strict per-pair payload schemas, exact no-fallback node resolution,
UPID submission/polling, SDK-proven synchronous result typing, timeout handling,
heartbeat/session overlap rejection, every declared write kind crossing the common fresh gate,
and `ProxmoxCephProviderAdapter.apply()` integration (fake `CephWrite`; no live
cluster).

Operational flow, failure recovery, rollout/rollback, and NPR 7150.2D Chapter 4
traceability live in `docs/operations/ceph-write-approvals.md`.
