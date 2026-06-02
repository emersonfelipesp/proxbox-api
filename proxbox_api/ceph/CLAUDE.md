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
| `v2_schemas.py` | Pydantic contract: `DesiredObject`/`DesiredStateBundle`, `PlanRequest`/`PlanResponse`, `ProviderOperation`, `ValidationResult`/`ValidationResponse`, `ApplyRequest`, `ReconcileRequest`, `OperationRun`, `ProviderCapabilities`/`CapabilitiesResponse`, `MetricsResponse`, `SSEEvent`. Requests expose a `branch_schema_id` property = `netbox_branch_schema_id or source_branch_schema_id`. |
| `v2_engine.py` | Plan/apply engine: `validate_payload`, `build_plan`, `remember_plan`/`get_plan` (in-process plan store), `apply_plan`, `reconcile_provider`, `record_to_operation_run`, `redact_secrets`. Deterministic ordered plans, capability-aware (unsupported ops are **blocked with a reason**, never silently skipped), destructive-op confirmation gating, idempotent re-apply (`completed_run_for_plan`), and run persistence. |
| `v2_providers/base.py` | `CephProviderAdapter` ABC (`capabilities`, `read_state`, `diff`, `plan`, `apply`, `reconcile`, `metrics`) + `CephCapabilityUnsupported`. |
| `v2_providers/proxmox.py` | `ProxmoxCephProviderAdapter` — read-only today (writes raise `CephCapabilityUnsupported`; enabled by proxmox-sdk #12). |
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
- The Proxmox adapter is read-only until proxmox-sdk #12 lands write-capable
  Ceph clients; Dashboard/RGW/RBD/Prometheus/external adapters arrive with
  #98/#12/#94/#97.

## Tests

`tests/ceph/test_v2_orchestration.py` — capabilities, validate, plan
build/get/404, happy-path apply, destructive-confirmation gating,
capability-blocked apply, idempotent re-apply, operation lookup/404, SSE shape,
reconcile, metrics (fake adapter; no live cluster).
