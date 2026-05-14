"""``POST /intent/plan`` — read-only validation of a branched NetBox merge.

Sub-PR D (#381) introduces the validator endpoint. The endpoint is
**read-only by design**: it inspects the proposed diffs against the
target Proxmox cluster's current state (node availability, storage
capacity, VMID collisions, cloud-init YAML well-formedness) and returns
per-diff verdicts. It MUST NOT mutate Proxmox.

The actual Proxmox SDK probes land in Sub-PRs F/G/K when the matching
apply paths land. Sub-PR D ships the contract and a permissive default
verdict so the merge_validator on the plugin side has something to call
end-to-end. Each later sub-PR fills in its slice of validation:

  * F — node online + storage capacity + VMID availability for CREATE.
  * G — TOCTOU node + VMID recheck for UPDATE.
  * K — cloud-init YAML parses + plaintext-password warning.
  * H — DELETE diffs are accepted at plan time; the four-eyes
        DeletionRequest workflow gates the actual destroy on
        ``/intent/deletion-requests/...``.

The route is authenticated like the rest of proxbox-api (the global
``APIKeyAuthMiddleware`` covers it). Writes are not performed, so the
``allow_writes`` gate is intentionally NOT applied — the gate would
block read-only plan calls in lab setups where ``allow_writes=False``
is the safe default. Sub-PRs F/G/H/I/K's apply/destroy routes call
``_gate()`` explicitly.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from proxbox_api.logger import logger
from proxbox_api.routes.intent.schemas import (
    IntentDiff,
    PlanRequest,
    PlanResponse,
    PlanVerdict,
)

router = APIRouter()


def _verdict_for(diff: IntentDiff) -> PlanVerdict:
    """Default-permit verdict.

    Sub-PRs F/G/H/K replace this with op-specific validation. We keep a
    permissive default so the plugin-side merge_validator can call this
    endpoint end-to-end during Sub-PR D without spurious blocks while
    later sub-PRs are still in flight.
    """
    if diff.op == "delete":
        return PlanVerdict(
            netbox_id=diff.netbox_id,
            op=diff.op,
            verdict="warning",
            reason="delete_routed_to_deletion_request",
            message=(
                "DELETE diffs land in a DeletionRequest for separate "
                "authorization (Sub-PR H). The merge will not call "
                "Proxmox destroy."
            ),
        )

    return PlanVerdict(
        netbox_id=diff.netbox_id,
        op=diff.op,
        verdict="permitted",
        reason="default_permit",
        message=("Plan-time validation accepted. Per-op probes land in Sub-PRs F/G/K."),
    )


@router.post(
    "/plan",
    response_model=PlanResponse,
    summary="Validate a NetBox→Proxmox intent merge",
    description=(
        "Read-only validation of the diffs in a netbox-branching branch "
        "before the operator confirms the merge. Returns per-diff verdicts; "
        "``permitted=False`` blocks the merge on the netbox-branching side."
    ),
)
async def plan(request: PlanRequest) -> JSONResponse:
    """Validate ``request.diffs`` and return per-diff verdicts."""
    verdicts: list[PlanVerdict] = [_verdict_for(diff) for diff in request.diffs]
    permitted = all(v.verdict != "blocked" for v in verdicts)

    if not request.diffs:
        summary = "No VM/LXC diffs in this branch — merge will be a no-op for Proxmox."
    else:
        counts = {"create": 0, "update": 0, "delete": 0}
        for diff in request.diffs:
            counts[diff.op] += 1
        summary = (
            f"{counts['create']} create / {counts['update']} update / "
            f"{counts['delete']} delete diff(s) classified."
        )

    logger.info(
        "intent.plan: endpoint=%s branch=%s actor=%s diffs=%d permitted=%s",
        request.endpoint_id,
        request.branch_id,
        request.actor,
        len(request.diffs),
        permitted,
    )

    response = PlanResponse(permitted=permitted, verdicts=verdicts, summary=summary)
    return JSONResponse(status_code=status.HTTP_200_OK, content=response.model_dump())
