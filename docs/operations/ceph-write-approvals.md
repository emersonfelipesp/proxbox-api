# Ceph v2 Write Approval and Recovery

Ceph v2 writes are fail-closed. A caller must create a durable plan for one
explicit Proxmox endpoint, obtain approval from a different actor, and consume
the returned one-time token before `proxbox-api` dispatches any mutation.
Planning and reconciliation remain read-only.

Write execution is also default-off at the service boundary. Both
`PROXBOX_ENABLE_CEPH_V2_WRITES=true` and
`PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY=true` are required before capability,
approval, or apply can grant mutation authority. The second flag is an operator
attestation that an authenticated gateway overwrites `X-Proxbox-Actor`; it is
not a substitute for deploying and verifying that gateway.

This guide covers the security contract introduced by issue #258. The v1
`/ceph/*` inventory routes are unchanged.

## Security boundary

The write path protects against these failure modes:

- selecting the first available Proxmox session instead of the endpoint named
  by the caller;
- advertising write capability when the endpoint is absent, disabled,
  ambiguous, or has `allow_writes=false`;
- changing an endpoint's write authorization after approval but before a later
  provider mutation;
- replacing the approved operations, branch, provider, endpoint, requester, or
  canonical plan payload at apply time;
- predictable confirmation strings, token disclosure from the database,
  expired approvals, replay, and concurrent duplicate delivery;
- one actor approving their own request; and
- losing the successful HTTP response and blindly issuing the mutation again.

The service API key remains the outer authentication boundary.
`X-Proxbox-Actor` is only a delegated assertion; this change does not
authenticate the named human. A trusted, authenticated NetBox gateway must
derive it from the authenticated principal and replace, rather than forward,
an untrusted client-supplied value. `proxbox-api` requires the header for plan,
approval, and apply, rejects blank values, and rejects a body `actor` that
disagrees with the header.

**Production rollout is blocked until that trusted gateway assertion is
deployed and verified.** Keep both Ceph execution flags false until then.
Direct clients that can choose both the service API key and actor header do not
satisfy independent two-person approval.

Ceph v2 validation and provider-error boundaries return fixed diagnostics.
Rejected input values, arbitrary extra-field names, and raw SDK/HTTP exception
text are not reflected into API, SSE, audit, or log output. Structured payloads
are recursively sanitized for normalized snake/camel/kebab/space variants of
secret-field aliases, including `access_key`, `rgw_access_key`, `accessKey`,
`apiKey`, `api_token`, `access_token`, `client_secret`, and `privateKey`;
`credential_ref` is kept only when it is a valid opaque identifier. Exception
objects and non-JSON provider values are converted only through the same
redacting boundary, never by persisting their raw fallback strings. The logger
recursively applies the same rule to deferred
format arguments, structured extras, URL userinfo/query credentials, nested
exception objects, and traceback text before a real handler renders them.

## Durable control-plane records

SQLite stores five related records:

| Table | Purpose |
|---|---|
| `ceph_plan` | Canonical plan payload, SHA-256 digest, endpoint ID, stable server-keyed endpoint configuration revision, requester, branch, and 15-minute validity window. Apply reloads this record and verifies every duplicated identity field plus the digest. |
| `ceph_approval` | One approval authority per plan. Stores only a SHA-256 token hash, the bound plan digest, endpoint ID and configuration revision, requester, distinct approver, expiry, consumption timestamp, consumer, and operation-run ID. |
| `ceph_operation_run` | Durable execution result, endpoint revision, provider task references, and an in-flight lease plus an unexposed random lease-owner nonce. The plan/endpoint/digest/requester/approver/approval identity fields are fixed when the run is created; lifecycle updates change only lease, status, and result fields. |
| `ceph_operation_event` | Ordered, append-only checkpoints. The engine records approval consumption, dispatch intent before each SDK call, UPID submission, and the observed terminal or `outcome_unknown` transition. `(run_id, sequence)` is unique. |
| `ceph_provider_task_claim` | Permanent provider-global ownership of each `(provider, provider_task_ref)`; `endpoint_id` remains audit context but is not part of identity. The unique constraint and first submission event commit atomically, so sequential or concurrent runs on the same or different endpoints cannot adopt and poll an earlier task as fresh execution evidence. The startup migration transactionally rebuilds collision-free endpoint-scoped legacy tables. If legacy rows/events contain the same provider reference on different endpoints, startup refuses with `ceph_provider_task_claim_cross_endpoint_collision` and leaves all evidence untouched for operator investigation. |

The raw approval token is returned only by the approval-creation response. It
is never written to the database, operation summary, log payload, or replay
response.

Safety timing settings are bounded and fall back to defaults when malformed or
non-finite: `PROXBOX_CEPH_TASK_TIMEOUT` defaults to 300 seconds (1–3600),
`PROXBOX_CEPH_TASK_POLL_INTERVAL` defaults to 1 second (0.1–60), and
`PROXBOX_CEPH_RUN_LEASE_SECONDS` defaults to 360 seconds (1–3600). Keep the
lease longer than any single provider-status request. The polling worker
renews it while the task is active but cannot reclaim it after expiry.

Plan IDs and approval IDs are identifiers, not credentials. Legacy booleans,
`confirm_destructive`, `confirmation_token`, and strings derived from a plan ID
do not authorize a write.

## Required flow

### 1. Inspect endpoint-scoped capability

Call `GET /ceph/v2/capabilities?provider=proxmox&endpoint_id=<id>`.
`apply=true` is returned only when all of these conditions hold at that moment:

- both Ceph execution flags are true and the trusted actor gateway has been
  operationally verified;
- the installed SDK exposes the required Ceph write surface;
- the endpoint exists and is enabled;
- exactly one enabled local database endpoint resolves by the durable ID and
  exactly one request-private session is created from its full connection,
  authentication, TLS, timeout, and retry schema; and
- the endpoint has `allow_writes=true`.

An unscoped Proxmox capability request is read/plan-only and returns
`apply=false`. Capability output is informational; the service repeats the
authorization checks at approval, apply, and immediately before every provider
mutation.

### 2. Create the canonical plan

Send `POST /ceph/v2/plans` (or the `/ceph/v2/plan` alias) with:

- a non-empty `X-Proxbox-Actor` requester;
- `provider="proxmox"`;
- an explicit positive `endpoint_id`; and
- the desired state or operations to plan.

`netbox-ceph` must resolve its plugin-side cluster endpoint to the canonical
proxbox-api SQLite endpoint ID through
`netbox_proxbox.views.backend_sync.resolve_backend_endpoint_id`; a NetBox plugin
primary key is not interchangeable with this value. Its canonical request has
top-level `provider="proxmox"` and `endpoint_id`, plus exactly one desired
object with the immutable `node` above a strict typed `payload`. Missing or
non-positive mappings stop before planning with `endpoint_id_required` or
schema validation; they must never fall back to another endpoint.

Every non-noop Proxmox operation in the resulting canonical plan must bind an
exact node in its top-level `node` field. A legacy desired payload may supply
`node`, but planning extracts it from the SDK argument payload and persists it
as the immutable operation binding. The adapter rejects a missing node, a node
that is not present in the selected endpoint, conflicting live/desired nodes,
and multi-node ambiguity. It never selects the first endpoint node and never
invents `localhost`. Delete operations retain the node binding even though the
desired `after_summary` is empty.

Planning validates `after_summary` against a strict Pydantic schema for the
exact `(kind, action)` pair and the provider sink validates it again. Unknown
keys and missing required fields block the plan instead of being filtered out
at dispatch. For example, OSD create requires `dev`, while OSD update requires
the boolean `in` field. Callers, including `netbox-ceph`, must therefore send
the exact node and only the documented SDK arguments for the intended pair.

The response includes `id`, `digest`, `endpoint_id`,
`endpoint_config_revision`, `requester`, `created_at`, and `expires_at`. The
revision is an opaque HMAC derived with the server credential-encryption key
from the complete mutation-relevant endpoint configuration; no endpoint secret
is persisted in it. The server persists the full response as the only apply
authority. Apply rejects replacement operations, desired state, scope,
provider, endpoint, endpoint revision, or branch values.

### 3. Obtain independent approval

A different authenticated actor sends:

```text
POST /ceph/v2/plans/{plan_id}/approvals
X-Proxbox-Actor: <approver>

{"endpoint_id": <same endpoint id>}
```

The plan must be current, valid, unblocked, and still write-authorized. One
canonical plan can issue only one approval authority, enforced by a database
unique constraint. The response is HTTP 201 and includes the opaque token once.
The token expires no later than ten minutes after issue and never later than
the plan itself.

The requester and approver comparison is case-insensitive. A second approval
POST returns HTTP 409 `approval_already_issued` with validated recovery metadata:
the existing approval ID, exact plan ID/digest, endpoint ID/revision,
requester/approver, and operation-run ID when consumed. It never returns the
token or hash. Use that approval ID with the status route; a new attempt still
requires a fresh plan.

### 4. Apply exactly once

The original requester sends:

```text
POST /ceph/v2/plans/{plan_id}/apply
X-Proxbox-Actor: <requester>

{
  "plan_id": "<same plan id>",
  "endpoint_id": <same endpoint id>,
  "approval_token": "<opaque token>"
}
```

`POST /ceph/v2/apply` accepts the same durable-plan envelope as a compatibility
alias. Inline build-and-apply is closed.

The engine consumes the approval with one conditional database update and
creates the bound `running` audit record in the same transaction. Concurrent
requests cannot both consume the token. Only the winner reaches the provider
adapter.

Immediately before each non-noop SDK call, the Proxmox adapter reloads the
endpoint gate, compares the persisted stable endpoint revision, and uses
constant-time HMAC comparisons against a per-request, never-persisted binding
of the endpoint and actual session schemas. Revoking
`allow_writes`, disabling/deleting the endpoint, or changing connection,
credential, TLS, timeout, retry, or session fields stops the next mutation. A
`noop` is not a provider mutation and does not invoke the write gate.

Before every SDK call, the engine durably appends a `dispatching` intent while
the run still owns a live lease. A background heartbeat renews that lease for
the whole SDK await. The Proxmox adapter's endpoint-freshness query and the
heartbeat serialize on one shared lock, so they never concurrently use the
request's SQLModel session. Every later checkpoint uses a compare-and-swap requiring
the same unexposed lease-owner nonce and a non-expired lease; a late worker
cannot append, terminalize, or reacquire a run it no longer owns. A process
crash leaves a nonterminal, auditable `dispatching` run until lease expiry,
when status/SSE recovery alone changes it to `outcome_unknown`. A returned UPID
means only `submitted`. The adapter polls the same node through the same bound
session: `stopped/OK` becomes `completed`, a non-OK terminal status becomes
`failed`, and transport failure, timeout, or cancellation becomes
`outcome_unknown`.

Each task-based Proxmox mutation must return exactly one complete Proxmox UPID
structure (node, three bounded hexadecimal time/process fields, task type, task
ID, authenticated user, and optional comment). The returned result node and
the UPID-embedded node must both equal the operation's immutable plan node, and
the full UPID must never have been claimed by any run for that provider,
regardless of endpoint. Missing, partial,
multiple, reused, or node-inconsistent task references append
`provider_task_binding_invalid` and make the run `outcome_unknown`; a string
that merely starts with `UPID:` is not execution evidence. Absence of a task ID
never means success. Only the proxmox-sdk-proven `flag:create`, `flag:update`,
`flag:delete`, and `osd:update` pairs accept a successful `None` response as an
explicit typed synchronous completion. Every other pair remains task-based;
`None` is `outcome_unknown`. The task-claim/submission transaction and explicit
synchronous-completion checkpoint are awaited through a repeated-cancellation
shield: every additional `cancel()` is remembered, the inner durability task
continues until terminal, and only then is cancellation re-raised. If
cancellation arrives after provider acceptance, the engine finishes both the
evidence and conservative `outcome_unknown` cancellation checkpoints first.

## Failure and recovery

Errors use a stable `detail.reason` value. Important cases include:

| HTTP | Reason | Meaning / action |
|---|---|---|
| 503 | `ceph_write_execution_disabled` | Keep approval/apply stopped. Deploy and verify the trusted actor gateway, then deliberately enable both service flags. |
| 400 | `actor_required` | Supply a non-empty trusted `X-Proxbox-Actor`. |
| 403 | `endpoint_disabled`, `endpoint_writes_disabled` | Keep writes stopped; correct endpoint policy deliberately and create a fresh plan if needed. |
| 403 | `approval_requester_mismatch` | Only the persisted plan requester may apply. |
| 404 | `endpoint_missing` | The durable selector no longer resolves. |
| 409 | `endpoint_configuration_changed`, `endpoint_session_ambiguous`, `endpoint_session_binding_mismatch`, `endpoint_session_binding_changed` | The endpoint ID was retargeted or its exact endpoint/session schema drifted. Preserve the record and create a fresh plan after deliberate correction. Never fall back to a generic or colliding session. |
| 409 | `plan_integrity_failed` | Treat the persisted plan as corrupt; preserve it for investigation and create a fresh plan. |
| 409 | `two_person_approval_required` | Approval must come from a distinct actor. |
| 409 | `approval_already_issued` | A plan has one approval authority. Use the validated recovery metadata and approval status route; create a fresh plan only for a deliberate new attempt. |
| 409 | `approval_invalid`, `approval_plan_mismatch`, `approval_endpoint_mismatch` | The credential is absent, unknown, or bound to different authority. Do not dispatch. |
| 409 | `approval_replayed` | The token was already consumed. Use the returned recovery IDs; do not retry the mutation. |
| 409 | `canonical_plan_required`, `persisted_plan_required` | Legacy inline or payload-replacement apply is closed. |
| 410 | `plan_expired`, `approval_expired` | Create and independently approve a fresh plan. |

When an apply response is lost, retrying the same request is safe: the service
returns HTTP 409 `approval_replayed` with `approval_id`, `plan_id`, and
`operation_run_id`. Read `GET /ceph/v2/operations/{operation_run_id}` and its
ordered `events`, or stream `GET /ceph/v2/operations/{id}/events`, to recover
the durable status. `GET /ceph/v2/approvals/{approval_id}` exposes only safe
metadata and the linked run ID; it never exposes the raw token or token hash.
Never treat the 409 as permission to issue another mutation.

If a `running`/`dispatching` run stops renewing its durable lease, the next
status/SSE read atomically changes it to `outcome_unknown`, appends a
`run_lease_expired` event, retains all task references, and returns an explicit
operator recovery action. Recovery clears the lease owner. A late SDK or
task-poll response cannot reacquire the expired lease, append a terminal event,
or overwrite that recovery state with `completed`. While the lease is still
live, lease loss never forces a premature terminal state: the authoritative
worker or later expiry recovery owns that transition. `completed` is reported
only after synchronous success or a terminal `stopped/OK` task observation. If
it is `failed`, the provider reported a known failure. If it is
`outcome_unknown`, do not retry: use read-only `POST /ceph/v2/reconcile` and the
provider task reference to establish actual cluster state. A deliberate
corrective write always starts with a new plan and a new independent approval.

## Reconciliation is read-only

`POST /ceph/v2/reconcile` reads provider state and records a reconciliation
summary. It does not call the Ceph writer and does not consume an approval.
Keep this invariant when adding provider capabilities; any new mutation kind
must pass through the common per-mutation write gate and the durable approval
engine.

Dashboard and external providers advertise `apply=false`,
`destructive_operations=false`, and false mutation-kind capabilities. Their
provider sinks also reject mutations. They remain closed until each provider
has a durable selector, configuration revision, credential authority, and an
equivalent fresh fail-closed write gate. Read, plan, metrics, and reconcile
support do not imply mutation authority.

## Upgrade and rollback

The schema change is additive: startup creates `ceph_plan`, `ceph_approval`,
`ceph_operation_event`, and `ceph_provider_task_claim`; adds endpoint configuration revisions to plan,
approval, and run records; and adds the run lease plus nullable `lease_owner`
to existing
`ceph_operation_run` tables. Legacy Proxmox plans/approvals without a revision
are invalid authority and fail closed. Existing task-bearing audit events are
idempotently backfilled into permanent claims before new apply traffic. The migration does not change any
endpoint's `allow_writes` value and does not delete old run history.

Use this rollout order:

1. Back up the SQLite database and keep Ceph `allow_writes=false` on all
   endpoints.
2. Deploy the backend and verify read, endpoint-scoped capability, plan, get,
   and read-only reconcile in staging.
3. Deploy the caller/UI that implements create → independent approve → apply
   and recovery by approval/run ID. Update the caller to provide one exact node
   and a strict per-kind/action payload for every Proxmox mutation. Verify that
   the authenticated gateway
   removes any client-supplied actor header and overwrites it from its trusted
   principal.
4. Set `PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY=true` only after that verification,
   then set `PROXBOX_ENABLE_CEPH_V2_WRITES=true` on staging. Enable writes on
   one staging endpoint, exercise a non-destructive plan, and
   prove that replay produces no second SDK call.
5. Complete the destructive-kind and authorization-revocation acceptance
   matrix before enabling a production endpoint deliberately.

For rollback, first block Ceph v2 approval/apply traffic and set endpoint write
authority false while the hardened version is still running. Preserve the new
tables and operation records. A pre-hardening binary must not be exposed to
Ceph v2 apply traffic, because it does not enforce this contract; isolate or
unmount the route before reverting application code. Additive columns and
tables can remain in place for a later roll-forward.

## NPR 7150.2D Chapter 4 evidence

Issue #258 and this guide provide **feature-level lifecycle evidence only**.
They do not establish project compliance, NASA certification/accreditation, a
software classification, or satisfaction of any SWE requirement by themselves.
The identifiers below are candidate trace links for a project authority to
review against the official applicability matrix and approved plans.

### Evidence produced by this change

| Phase | Candidate SWE links | Bounded artifact |
|---|---|---|
| Requirements and risk | SWE-050, SWE-051, SWE-053, SWE-054, SWE-055, SWE-184 | Issue/change record, threat boundary, acceptance criteria, endpoint substitution/replay/secret/cancellation tests. This is not a complete project requirements baseline or bidirectional traceability matrix. |
| Architecture and design | SWE-057, SWE-058 | Documented plan → approval → atomic consumption → endpoint revision/session gate → event/lease state machine. No formal project architecture review or approved design baseline is claimed. |
| Implementation | SWE-060, SWE-061, SWE-062, SWE-135, SWE-136, SWE-186 | Typed Pydantic/SQLModel implementation, additive migration, Ruff/compile/test commands, pinned dependency lock, and concurrency tests. Tool accreditation and project-wide coding/analysis records remain outside this change. |
| Testing | SWE-065, SWE-066, SWE-068, SWE-071, SWE-187, SWE-189, SWE-190, SWE-191, SWE-192, SWE-193, SWE-211 | Focused unit/HTTP/concurrency/migration/security canaries, including exact node binding, canonical node-free no-op comparison, strict payload rejection, lease-owner CAS/non-resurrection, durable sequential/concurrent task-claim uniqueness, cancellation-safe task/synchronous checkpoints, serialized heartbeat/session access, explicit synchronous-pair completion, recursive API/SSE/persistence/log redaction through a real handler, and recorded local results. This is not target-platform qualification, a project hazard matrix, full coverage disposition, or release acceptance. |
| Operations and maintenance | SWE-075, SWE-077, SWE-194, SWE-195, SWE-196 | Rollout, rollback, fail-closed recovery, audit fields, and operator gates are documented. Delivery approval, archive ownership/access, retention execution, and retirement records are not established here. |

### Explicit open dispositions

- **Applicability/classification:** a responsible project authority must assign
  the software classification and determine applicable SWE requirements. This
  branch does not mark SWE-143 or any other requirement N/A.
- **Independent lifecycle records:** no approved project software management
  plan, requirements baseline, architecture/design review minutes,
  configuration status accounting report, tool qualification/accreditation
  record, or project-level requirements-test matrix is supplied by this branch.
- **Verification:** the Python 3.12 `AsyncSession` race, full repository CI,
  branch coverage report/disposition, independent adversarial re-review, and
  trusted-gateway/staging validation remain pre-merge or pre-delivery gates.
- **Target operation:** no live Ceph mutation or target-platform qualification
  was performed. Production execution remains disabled.
- **Release/delivery/archive:** SWE-063 release description, delivery approval,
  defect closure, archive custody/access, maintenance execution, and retirement
  evidence belong to the controlled release/project process and remain open.

Release evidence should record the exact test commands and results, reviewed
diff, migration backup, staging endpoint ID, approval/run IDs with secrets
redacted, and the operator decision that enabled production writes.
