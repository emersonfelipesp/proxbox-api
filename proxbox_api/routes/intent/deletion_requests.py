"""Deletion-request approval and execution routes."""

from __future__ import annotations

import json
import time
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import DeletionRequestRecord
from proxbox_api.routes.intent.dispatchers.common import (
    IntentEndpointContext,
    write_deletion_request_journal,
)
from proxbox_api.routes.intent.dispatchers.lxc_destroy import dispatch_lxc_destroy
from proxbox_api.routes.intent.dispatchers.qemu_destroy import dispatch_qemu_destroy
from proxbox_api.routes.intent.schemas import (
    DeletionRequestExecuteResponse,
    DeletionRequestReject,
    DeletionRequestResponse,
    DeletionRequestTarget,
)
from proxbox_api.routes.proxmox_actions import _gate
from proxbox_api.services.verb_dispatch import write_verb_journal_entry
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter(prefix="/deletion-requests")


def _actor_label(value: str | None) -> str:
    return value or "proxbox-api"


def _gate_reason(response: JSONResponse) -> str:
    try:
        body = json.loads(response.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return "gate_rejected"
    reason = body.get("reason") if isinstance(body, dict) else None
    return str(reason) if reason else "gate_rejected"


def _response(record: DeletionRequestRecord) -> DeletionRequestResponse:
    assert record.id is not None
    return DeletionRequestResponse(
        id=record.id,
        endpoint_id=record.endpoint_id,
        vmid=record.vmid,
        node=record.node,
        kind=record.kind,  # type: ignore[arg-type]
        state=record.state,  # type: ignore[arg-type]
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def _get_record(session: object, deletion_request_id: int) -> DeletionRequestRecord:
    record = await _maybe_await(session.get(DeletionRequestRecord, deletion_request_id))
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No deletion request with id={deletion_request_id}.",
        )
    return record


async def _save_record(session: object, record: DeletionRequestRecord) -> None:
    record.updated_at = time.time()
    session.add(record)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(record))


async def _write_route_journal(
    *,
    session: object,
    endpoint: object | None,
    record: DeletionRequestRecord,
    verb: str,
    result: str,
    actor: str,
    run_uuid: str,
    journal_kind: str,
    reason: str | None = None,
    error_detail: str | None = None,
) -> None:
    await write_deletion_request_journal(
        journal_writer=write_verb_journal_entry,
        session=session,
        endpoint=endpoint,
        endpoint_id=record.endpoint_id,
        verb=verb,
        result=result,
        vmid=record.vmid,
        actor=actor,
        run_uuid=run_uuid,
        target_kind=record.kind,
        journal_kind=journal_kind,
        deletion_request_id=record.id,
        reason=reason,
        error_detail=error_detail,
    )


def _target_mismatch(record: DeletionRequestRecord, body: DeletionRequestTarget) -> str | None:
    if record.vmid != body.vmid:
        return f"vmid mismatch: request has {record.vmid}, body has {body.vmid}"
    if record.node != body.node:
        return f"node mismatch: request has {record.node!r}, body has {body.node!r}"
    if record.kind != body.kind:
        return f"kind mismatch: request has {record.kind!r}, body has {body.kind!r}"
    return None


async def _gated_or_journaled(
    *,
    session: object,
    record: DeletionRequestRecord,
    verb: str,
    actor: str,
    run_uuid: str,
) -> JSONResponse | object:
    gated = await _gate(session, record.endpoint_id)
    if isinstance(gated, JSONResponse):
        reason = _gate_reason(gated)
        await _write_route_journal(
            session=session,
            endpoint=None,
            record=record,
            verb=verb,
            result="blocked",
            actor=actor,
            run_uuid=run_uuid,
            journal_kind="warning",
            reason=reason,
            error_detail=reason,
        )
    return gated


@router.post(
    "/{deletion_request_id}/approve",
    response_model=DeletionRequestResponse,
    summary="Approve a pending deletion request",
)
async def approve_deletion_request(
    deletion_request_id: int,
    body: DeletionRequestTarget,
    session: SessionDep,
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> DeletionRequestResponse | JSONResponse:
    run_uuid = str(uuid4())
    actor_label = _actor_label(actor)
    record = await _get_record(session, deletion_request_id)
    gated = await _gated_or_journaled(
        session=session,
        record=record,
        verb="deletion_request_approve",
        actor=actor_label,
        run_uuid=run_uuid,
    )
    if isinstance(gated, JSONResponse):
        return gated

    mismatch = _target_mismatch(record, body)
    if mismatch is not None:
        await _write_route_journal(
            session=session,
            endpoint=gated,
            record=record,
            verb="deletion_request_approve",
            result="failed",
            actor=actor_label,
            run_uuid=run_uuid,
            journal_kind="warning",
            reason="target_mismatch",
            error_detail=mismatch,
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=mismatch)

    if record.state not in {"pending", "approved"}:
        message = f"Deletion request is {record.state}; only pending requests can be approved."
        await _write_route_journal(
            session=session,
            endpoint=gated,
            record=record,
            verb="deletion_request_approve",
            result="failed",
            actor=actor_label,
            run_uuid=run_uuid,
            journal_kind="warning",
            reason="invalid_state",
            error_detail=message,
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)

    record.state = "approved"
    await _save_record(session, record)
    await _write_route_journal(
        session=session,
        endpoint=gated,
        record=record,
        verb="deletion_request_approve",
        result="succeeded",
        actor=actor_label,
        run_uuid=run_uuid,
        journal_kind="success",
    )
    return _response(record)


@router.post(
    "/{deletion_request_id}/reject",
    response_model=DeletionRequestResponse,
    summary="Reject a deletion request",
)
async def reject_deletion_request(
    deletion_request_id: int,
    body: DeletionRequestReject,
    session: SessionDep,
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> DeletionRequestResponse | JSONResponse:
    run_uuid = str(uuid4())
    actor_label = _actor_label(actor)
    record = await _get_record(session, deletion_request_id)
    gated = await _gated_or_journaled(
        session=session,
        record=record,
        verb="deletion_request_reject",
        actor=actor_label,
        run_uuid=run_uuid,
    )
    if isinstance(gated, JSONResponse):
        return gated

    if record.state in {"executing", "succeeded"}:
        message = f"Deletion request is {record.state}; it can no longer be rejected."
        await _write_route_journal(
            session=session,
            endpoint=gated,
            record=record,
            verb="deletion_request_reject",
            result="failed",
            actor=actor_label,
            run_uuid=run_uuid,
            journal_kind="warning",
            reason="invalid_state",
            error_detail=message,
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)

    record.state = "rejected"
    await _save_record(session, record)
    await _write_route_journal(
        session=session,
        endpoint=gated,
        record=record,
        verb="deletion_request_reject",
        result="succeeded",
        actor=actor_label,
        run_uuid=run_uuid,
        journal_kind="warning",
        reason=body.reason,
    )
    return _response(record)


@router.post(
    "/{deletion_request_id}/execute",
    response_model=DeletionRequestExecuteResponse,
    summary="Execute an approved deletion request",
)
async def execute_deletion_request(
    deletion_request_id: int,
    body: DeletionRequestTarget,
    session: SessionDep,
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> DeletionRequestExecuteResponse | JSONResponse:
    run_uuid = str(uuid4())
    actor_label = _actor_label(actor)
    record = await _get_record(session, deletion_request_id)
    gated = await _gated_or_journaled(
        session=session,
        record=record,
        verb="deletion_request_execute",
        actor=actor_label,
        run_uuid=run_uuid,
    )
    if isinstance(gated, JSONResponse):
        return gated

    mismatch = _target_mismatch(record, body)
    if mismatch is not None:
        await _write_route_journal(
            session=session,
            endpoint=gated,
            record=record,
            verb="deletion_request_execute",
            result="failed",
            actor=actor_label,
            run_uuid=run_uuid,
            journal_kind="warning",
            reason="target_mismatch",
            error_detail=mismatch,
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=mismatch)

    if record.state != "approved":
        message = f"Deletion request is {record.state}; execute requires approved."
        await _write_route_journal(
            session=session,
            endpoint=gated,
            record=record,
            verb="deletion_request_execute",
            result="failed",
            actor=actor_label,
            run_uuid=run_uuid,
            journal_kind="warning",
            reason="invalid_state",
            error_detail=message,
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)

    record.state = "executing"
    await _save_record(session, record)
    endpoint_context = IntentEndpointContext(session=session, endpoint_id=record.endpoint_id)

    try:
        if body.kind == "qemu":
            result = await dispatch_qemu_destroy(
                endpoint_context,
                body.vmid,
                body.node,
                run_uuid,
                actor=actor_label,
            )
        else:
            result = await dispatch_lxc_destroy(
                endpoint_context,
                body.vmid,
                body.node,
                run_uuid,
                actor=actor_label,
            )
    except Exception:
        record.state = "failed"
        await _save_record(session, record)
        raise

    record.state = "succeeded"
    await _save_record(session, record)
    return DeletionRequestExecuteResponse(
        upid=result.get("upid"),
        run_uuid=str(result.get("run_uuid") or run_uuid),
    )
