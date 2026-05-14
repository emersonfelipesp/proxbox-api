"""``POST /intent/apply`` — NetBox→Proxmox intent apply."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.logger import logger
from proxbox_api.routes.intent.dispatchers.common import (
    IntentEndpointContext,
    scrub_message,
    write_intent_journal,
)
from proxbox_api.routes.intent.dispatchers.lxc_create import dispatch_lxc_create
from proxbox_api.routes.intent.dispatchers.lxc_update import dispatch_lxc_update
from proxbox_api.routes.intent.dispatchers.qemu_create import dispatch_qemu_create
from proxbox_api.routes.intent.dispatchers.qemu_update import dispatch_qemu_update
from proxbox_api.routes.intent.schemas import (
    ApplyDiff,
    ApplyRequest,
    ApplyResponse,
    ApplyResultItem,
    LXCIntentPayload,
    VMIntentPayload,
)
from proxbox_api.runtime_settings import get_bool
from proxbox_api.services.verb_dispatch import write_verb_journal_entry

router = APIRouter()


def _overall(results: list[ApplyResultItem]) -> str:
    if not results:
        return "no_op"
    if all(item.status in {"succeeded", "skipped"} for item in results):
        return "succeeded"
    if all(item.status == "failed" for item in results):
        return "failed"
    return "partial"


def _vmid(diff: ApplyDiff) -> int:
    return diff.payload.vmid


def _expose_internal_errors() -> bool:
    return get_bool(
        settings_key="expose_internal_errors",
        env="PROXBOX_EXPOSE_INTERNAL_ERRORS",
        default=False,
    )


async def _write_apply_failure_journal(
    *,
    session: object,
    endpoint_id: int | None,
    body: ApplyRequest,
    error_detail: str,
) -> None:
    context = IntentEndpointContext(session=session, endpoint_id=endpoint_id)
    await write_intent_journal(
        journal_writer=write_verb_journal_entry,
        endpoint_context=context,
        endpoint=None,
        verb="intent_apply",
        result="failed",
        vmid=0,
        actor=body.actor,
        run_uuid=body.run_uuid,
        kind="warning",
        error_detail=error_detail,
    )


def _payload_failure(diff: ApplyDiff, message: str) -> ApplyResultItem:
    return ApplyResultItem(
        netbox_id=diff.netbox_id,
        vmid=_vmid(diff),
        op=diff.op,
        kind=diff.kind,
        status="failed",
        message=message,
    )


def _writes_disabled_response(result: ApplyResultItem) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "reason": result.reason,
            "detail": result.message,
        },
    )


async def _dispatch_update_diff(
    *,
    diff: ApplyDiff,
    endpoint_context: IntentEndpointContext,
    actor: str | None,
    run_uuid: str,
) -> ApplyResultItem:
    if diff.kind == "qemu":
        if not isinstance(diff.payload, VMIntentPayload):
            return _payload_failure(diff, "qemu update payload requires VMIntentPayload")
        return await dispatch_qemu_update(
            endpoint_context,
            diff.payload,
            run_uuid,
            actor=actor,
        )

    if not isinstance(diff.payload, LXCIntentPayload):
        return _payload_failure(diff, "lxc update payload requires LXCIntentPayload")
    return await dispatch_lxc_update(
        endpoint_context,
        diff.payload,
        run_uuid,
        actor=actor,
    )


@router.post(
    "/apply",
    response_model=ApplyResponse,
    summary="Apply NetBox→Proxmox intent diffs",
)
async def apply_intent(
    request: Request,
    body: ApplyRequest,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> ApplyResponse | JSONResponse:
    del request
    try:
        results: list[ApplyResultItem] = []
        for diff in body.diffs:
            endpoint_context = IntentEndpointContext(
                session=session,
                endpoint_id=endpoint_id,
                netbox_id=diff.netbox_id,
            )
            if diff.op == "create" and diff.kind == "qemu":
                if not isinstance(diff.payload, VMIntentPayload):
                    results.append(
                        _payload_failure(diff, "qemu create payload requires VMIntentPayload")
                    )
                    continue
                results.append(
                    await dispatch_qemu_create(
                        diff.payload,
                        endpoint=endpoint_context,
                        actor=body.actor,
                        run_uuid=body.run_uuid,
                    )
                )
                continue

            if diff.op == "create" and diff.kind == "lxc":
                if not isinstance(diff.payload, LXCIntentPayload):
                    results.append(
                        _payload_failure(diff, "lxc create payload requires LXCIntentPayload")
                    )
                    continue
                results.append(
                    await dispatch_lxc_create(
                        diff.payload,
                        endpoint=endpoint_context,
                        actor=body.actor,
                        run_uuid=body.run_uuid,
                    )
                )
                continue

            if diff.op == "update":
                result = await _dispatch_update_diff(
                    diff=diff,
                    endpoint_context=endpoint_context,
                    actor=body.actor,
                    run_uuid=body.run_uuid,
                )
                if result.reason == "writes_disabled_for_endpoint":
                    return _writes_disabled_response(result)
                results.append(result)
                continue

            results.append(
                ApplyResultItem(
                    netbox_id=diff.netbox_id,
                    vmid=_vmid(diff),
                    op="delete",
                    kind=diff.kind,
                    status="not_implemented",
                    message="DELETE goes through DeletionRequest (Sub-PRs H/I, #385/#386)",
                )
            )

        return ApplyResponse(
            run_uuid=body.run_uuid,
            overall=_overall(results),
            results=results,
        )
    except HTTPException:
        raise
    except Exception as error:
        raw_body = body.model_dump(mode="python")
        safe_error = scrub_message(str(error), raw_body)
        logger.exception(
            "intent.apply: unexpected failure run_uuid=%s error=%s",
            body.run_uuid,
            safe_error,
        )
        await _write_apply_failure_journal(
            session=session,
            endpoint_id=endpoint_id,
            body=body,
            error_detail=safe_error,
        )
        detail = safe_error if _expose_internal_errors() else "internal_error"
        raise HTTPException(status_code=500, detail=detail) from error
