# proxbox_api/ceph Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/ceph/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.

---

## Purpose

Ceph integration surface for `proxbox-api`. Two layers:

- **v1 (`/ceph`)** — read-only reflected inventory of Proxmox-managed Ceph
  (`routes.py`, `schemas.py`, `client.py`). Status + sync endpoints; every sync
  route accepts `netbox_branch_schema_id`.
- **v2 (`/ceph/v2`)** — the NetBox-driven control plane (issue #95). Lets NetBox
  fully configure Ceph desired-state without direct operator access to Proxmox
  or external Ceph tooling.

Both routers are mounted in `proxbox_api/app/factory.py` (`/ceph` and `/ceph/v2`).

## v2 Files

| File | Role |
|------|------|
| `v2_schemas.py` | Pydantic contract: `DesiredObject`/`DesiredStateBundle`, `PlanRequest`/`PlanResponse`, `ProviderOperation`, `ValidationResult`/`ValidationResponse`, `ApplyRequest`, `ReconcileRequest`, `OperationRun`, `ProviderCapabilities`/`CapabilitiesResponse`, `MetricsResponse`, `SSEEvent`. Requests expose a `branch_schema_id` property = `netbox_branch_schema_id or source_branch_schema_id`. `PlanRequest`/`ApplyRequest` before-validators accept the **netbox-ceph `CephOperation` shape** (`_coerce_netbox_operation_payload`): `{operation_type, target_kind, target_ref, desired, provider_kind, confirmed, ...}` is adapted into a one-object `DesiredStateBundle` (`kind=target_kind`, `action=operation_type`, `payload=desired`, `provider=provider_kind`); `ApplyRequest` also maps `confirmed`→`confirm_destructive`. (#226) |
| `v2_engine.py` | Plan/apply engine: `validate_payload`, `build_plan`, `remember_plan`/`get_plan` (in-process plan store), `apply_plan`, `reconcile_provider`, `record_to_operation_run`, `redact_secrets`. Deterministic ordered plans, capability-aware (unsupported ops are **blocked with a reason**, never silently skipped), destructive-op confirmation gating, idempotent re-apply (`completed_run_for_plan`), and run persistence. |
| `v2_providers/base.py` | `CephProviderAdapter` ABC (`capabilities`, `read_state`, `diff`, `plan`, `apply`, `reconcile`, `metrics`) + `CephCapabilityUnsupported`. |
| `v2_providers/proxmox.py` | `ProxmoxCephProviderAdapter` — reads/diffs/plans **and applies** PVE-managed Ceph writes. `apply()` resolves a per-node `CephClient` and dispatches through `proxmox_writer`. Write capability is guarded: `capabilities().apply` and the write `operation_kinds` are `True` only when the installed proxmox-sdk ships `CephWrite` (`cephwrite_importable()`), so an older pin degrades cleanly instead of silently no-op'ing. |
| `v2_providers/proxmox_writer.py` | `(kind, action) -> CephWrite` mapping (#224): `execute_operation()`, `resolve_node()`, `operation_kinds()`, `cephwrite_importable()`. Supports pool create/update/delete, flag set/unset, OSD create/delete/in/out, MON/MGR/MDS create/delete, CephFS create. Destructive ops thread `confirm_destructive` into `CephWrite(confirm_destroy=)`. `filesystem:delete` and `crush_rule:*` are reported unsupported (no silent gaps). Returns `{upid, operation_id, result, target_ref, ...}`; the engine harvests `upid`. |
| `v2_providers/__init__.py` | Adapter registry: `adapter_for_provider(provider, pxs)`, `provider_names()`. Stub adapters for `dashboard` (#98), `rgw_admin` (#12), `rbd` (#12), `prometheus` (#94), `external` (#97). |
| `v2_routes.py` | FastAPI router for all `/ceph/v2/*` endpoints (thin; delegates to the engine). |

## Endpoint Map (`/ceph/v2`)

| Method + Path | Purpose |
|---|---|
| `GET /capabilities` | Per-provider capability flags for UI gating |
| `POST /validate` | Validate a desired object or full bundle |
| `POST /plans`, `GET /plans/{id}`, `POST /plans/{id}/apply` | Resource-style plan build / inspect / apply |
| `POST /plan`, `POST /apply` | Flat client-contract aliases the `netbox-ceph` orchestrator calls |
| `GET /operations/{id}` | Operation run status, task refs, warnings, errors, result |
| `GET /operations/{id}/events` | SSE operation-progress stream (`text/event-stream`) |
| `POST /reconcile` | Reconcile provider state back into NetBox summaries |
| `GET /metrics` | Latest provider metrics for a scope |

Persistence: `CephOperationRunRecord` (SQLModel, `ceph_operation_run` table in
`proxbox_api/database.py`), created by the standard `SQLModel.metadata.create_all`.

## Rules

- **Secrets never reach NetBox.** Providers resolve real credentials behind the
  adapter layer; the engine runs `redact_secrets()` on stored/logged payloads.
  NetBox passes only opaque `credential_ref` pointers.
- **No silent capability gaps.** Unsupported operations are surfaced as blocked
  with a reason; destructive operations require explicit confirmation.
- **Thread the branch id.** Pass `netbox_branch_schema_id` through
  plan/apply/reconcile so branch-aware NetBox staging stays consistent.
- v2 is additive; do not change v1 `/ceph` behavior.
- The Proxmox adapter now executes PVE Ceph writes through `proxmox_writer` +
  proxmox-sdk `CephWrite` (#12 part 1 / #224). Live execution requires the
  proxbox-api `proxmox-sdk` pin to include `CephWrite`; until that pin bump the
  adapter advertises `apply=False` and blocks writes with a clear reason.
  Dashboard/RGW/RBD/Prometheus/external adapters arrive with #98/#12/#94/#97.

## Tests

`tests/ceph/test_v2_orchestration.py` — capabilities, validate, plan
build/get/404, happy-path apply, destructive-confirmation gating,
capability-blocked apply, idempotent re-apply, operation lookup/404, SSE shape,
reconcile, metrics (fake adapter; no live cluster).

`tests/ceph/test_v2_proxmox_writer.py` — the `(kind, action) -> CephWrite`
mapping, destructive-confirmation threading, node resolution, UPID surfacing,
capability gating, and `ProxmoxCephProviderAdapter.apply()` integration (fake
`CephWrite`; no live cluster).
