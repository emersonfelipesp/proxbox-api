"""CREATE dispatcher for QEMU intent diffs."""

from __future__ import annotations

from fastapi.responses import JSONResponse

from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.routes.intent.cloud_init import build_proxmox_ci_args
from proxbox_api.routes.intent.dispatchers.common import (
    coerce_endpoint_context,
    extract_upid,
    merge_indexed_items,
    scrub_message,
    scrub_value,
    write_intent_journal,
)
from proxbox_api.routes.intent.schemas import ApplyResultItem, VMIntentPayload
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.services.verb_dispatch import write_verb_journal_entry


def _build_qemu_create_params(payload: VMIntentPayload) -> dict[str, object]:
    params: dict[str, object] = {
        "vmid": payload.vmid,
        "name": payload.name,
    }
    if payload.cores is not None:
        params["cores"] = payload.cores
    if payload.memory_mib is not None:
        params["memory"] = payload.memory_mib
    if payload.tags:
        params["tags"] = ";".join(payload.tags)
    if payload.cloud_init is not None:
        params.update(build_proxmox_ci_args(payload.cloud_init))
    merge_indexed_items(params, payload.disks, default_prefix="scsi")
    merge_indexed_items(params, payload.nics, default_prefix="net")
    return params


async def dispatch_qemu_create(
    payload: VMIntentPayload,
    *,
    endpoint,
    actor: str | None,
    run_uuid: str,
) -> ApplyResultItem:
    endpoint_context = coerce_endpoint_context(endpoint)
    gated = await _gate(endpoint_context.session, endpoint_context.endpoint_id)
    if isinstance(gated, JSONResponse):
        await write_intent_journal(
            journal_writer=write_verb_journal_entry,
            endpoint_context=endpoint_context,
            endpoint=None,
            verb="intent_create_qemu",
            result="blocked",
            vmid=payload.vmid,
            actor=actor,
            run_uuid=run_uuid,
            kind="warning",
            error_detail="writes_disabled_for_endpoint",
        )
        return ApplyResultItem(
            netbox_id=endpoint_context.netbox_id,
            vmid=payload.vmid,
            op="create",
            kind="qemu",
            status="failed",
            message="writes_disabled_for_endpoint",
        )

    if payload.name is None:
        await write_intent_journal(
            journal_writer=write_verb_journal_entry,
            endpoint_context=endpoint_context,
            endpoint=gated,
            verb="intent_create_qemu",
            result="failed",
            vmid=payload.vmid,
            actor=actor,
            run_uuid=run_uuid,
            kind="warning",
            error_detail="name required for QEMU create",
        )
        return ApplyResultItem(
            netbox_id=endpoint_context.netbox_id,
            vmid=payload.vmid,
            op="create",
            kind="qemu",
            status="failed",
            message="name required for QEMU create",
        )

    if payload.template_vmid is not None:
        return ApplyResultItem(
            netbox_id=endpoint_context.netbox_id,
            vmid=payload.vmid,
            op="create",
            kind="qemu",
            status="not_implemented",
            message="template clone lands in Sub-PR K",
        )

    raw_payload = payload.model_dump(mode="python")
    safe_payload = scrub_value(raw_payload)
    try:
        logger.debug(
            "intent.apply: qemu create dispatching vmid=%s payload=%s",
            payload.vmid,
            safe_payload,
        )
        proxmox = await _open_proxmox_session(gated)
        params = _build_qemu_create_params(payload)
        response = await resolve_async(proxmox.session.nodes(payload.node).qemu.post(**params))
        upid = extract_upid(response)
    except Exception as error:  # noqa: BLE001
        message = scrub_message(str(error), raw_payload)
        logger.warning(
            "intent.apply: qemu create failed vmid=%s node=%s error=%s",
            payload.vmid,
            payload.node,
            message,
        )
        await write_intent_journal(
            journal_writer=write_verb_journal_entry,
            endpoint_context=endpoint_context,
            endpoint=gated,
            verb="intent_create_qemu",
            result="failed",
            vmid=payload.vmid,
            actor=actor,
            run_uuid=run_uuid,
            kind="warning",
            error_detail=message,
        )
        return ApplyResultItem(
            netbox_id=endpoint_context.netbox_id,
            vmid=payload.vmid,
            op="create",
            kind="qemu",
            status="failed",
            message=message,
        )

    await write_intent_journal(
        journal_writer=write_verb_journal_entry,
        endpoint_context=endpoint_context,
        endpoint=gated,
        verb="intent_create_qemu",
        result="succeeded",
        vmid=payload.vmid,
        actor=actor,
        run_uuid=run_uuid,
        kind="success",
        proxmox_upid=upid,
    )
    return ApplyResultItem(
        netbox_id=endpoint_context.netbox_id,
        vmid=payload.vmid,
        op="create",
        kind="qemu",
        status="succeeded",
        proxmox_upid=upid,
    )
