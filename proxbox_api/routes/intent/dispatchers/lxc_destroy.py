"""Destroy dispatcher for LXC deletion requests."""

from __future__ import annotations

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse

from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.routes.intent.dispatchers.common import (
    coerce_endpoint_context,
    extract_upid,
    scrub_message,
    write_deletion_request_journal,
)
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.services.verb_dispatch import write_verb_journal_entry


async def dispatch_lxc_destroy(endpoint, vmid: int, node: str, run_uuid, *, actor) -> dict:
    endpoint_context = coerce_endpoint_context(endpoint)
    gated = await _gate(endpoint_context.session, endpoint_context.endpoint_id)
    if isinstance(gated, JSONResponse):
        await write_deletion_request_journal(
            journal_writer=write_verb_journal_entry,
            session=endpoint_context.session,
            endpoint=None,
            endpoint_id=endpoint_context.endpoint_id or 0,
            verb="deletion_request_execute",
            result="blocked",
            vmid=vmid,
            actor=actor,
            run_uuid=str(run_uuid),
            target_kind="lxc",
            journal_kind="warning",
            error_detail="writes_disabled_for_endpoint",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="writes_disabled_for_endpoint"
        )

    try:
        logger.info("intent.deletion_requests: lxc destroy dispatching vmid=%s node=%s", vmid, node)
        proxmox = await _open_proxmox_session(gated)
        response = await resolve_async(
            proxmox.session.delete("nodes", node, "lxc", str(vmid), purge=1)
        )
        upid = extract_upid(response)
    except Exception as error:  # noqa: BLE001
        message = scrub_message(str(error))
        logger.warning(
            "intent.deletion_requests: lxc destroy failed vmid=%s node=%s error=%s",
            vmid,
            node,
            message,
        )
        await write_deletion_request_journal(
            journal_writer=write_verb_journal_entry,
            session=endpoint_context.session,
            endpoint=gated,
            endpoint_id=gated.id or endpoint_context.endpoint_id or 0,
            verb="deletion_request_execute",
            result="failed",
            vmid=vmid,
            actor=actor,
            run_uuid=str(run_uuid),
            target_kind="lxc",
            journal_kind="warning",
            error_detail=message,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message) from error

    await write_deletion_request_journal(
        journal_writer=write_verb_journal_entry,
        session=endpoint_context.session,
        endpoint=gated,
        endpoint_id=gated.id or endpoint_context.endpoint_id or 0,
        verb="deletion_request_execute",
        result="succeeded",
        vmid=vmid,
        actor=actor,
        run_uuid=str(run_uuid),
        target_kind="lxc",
        journal_kind="success",
        proxmox_upid=upid,
    )
    return {"upid": upid, "run_uuid": str(run_uuid)}
