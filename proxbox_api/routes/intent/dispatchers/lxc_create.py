"""CREATE dispatcher for LXC intent diffs."""

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
from proxbox_api.routes.intent.schemas import ApplyResultItem, LXCIntentPayload
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.services.verb_dispatch import write_verb_journal_entry


def _scrub_lxc_payload(payload: LXCIntentPayload) -> dict[str, object]:
    payload_dict = payload.model_dump(mode="python")
    payload_dict["password"] = "***" if payload_dict.get("password") else None
    # TODO Sub-PR K: use log_scrubbing utility when it lands.
    return scrub_value(payload_dict)  # type: ignore[return-value]


def _build_lxc_create_params(payload: LXCIntentPayload) -> dict[str, object]:
    params: dict[str, object] = {
        "vmid": payload.vmid,
        "hostname": payload.hostname,
        "ostemplate": payload.ostemplate,
    }
    if payload.cores is not None:
        params["cores"] = payload.cores
    if payload.memory_mib is not None:
        params["memory"] = payload.memory_mib
    if payload.storage is not None:
        params["storage"] = payload.storage
    if payload.password is not None:
        params["password"] = payload.password
    if payload.tags:
        params["tags"] = ";".join(payload.tags)
    if payload.cloud_init is not None:
        params.update(build_proxmox_ci_args(payload.cloud_init))
    merge_indexed_items(params, payload.disks, default_prefix="mp")
    merge_indexed_items(params, payload.nics, default_prefix="net")
    return params


async def dispatch_lxc_create(
    payload: LXCIntentPayload,
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
            verb="intent_create_lxc",
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
            kind="lxc",
            status="failed",
            message="writes_disabled_for_endpoint",
        )

    if payload.hostname is None:
        await write_intent_journal(
            journal_writer=write_verb_journal_entry,
            endpoint_context=endpoint_context,
            endpoint=gated,
            verb="intent_create_lxc",
            result="failed",
            vmid=payload.vmid,
            actor=actor,
            run_uuid=run_uuid,
            kind="warning",
            error_detail="hostname required for LXC create",
        )
        return ApplyResultItem(
            netbox_id=endpoint_context.netbox_id,
            vmid=payload.vmid,
            op="create",
            kind="lxc",
            status="failed",
            message="hostname required for LXC create",
        )

    if payload.ostemplate is None:
        await write_intent_journal(
            journal_writer=write_verb_journal_entry,
            endpoint_context=endpoint_context,
            endpoint=gated,
            verb="intent_create_lxc",
            result="failed",
            vmid=payload.vmid,
            actor=actor,
            run_uuid=run_uuid,
            kind="warning",
            error_detail="ostemplate required for LXC create",
        )
        return ApplyResultItem(
            netbox_id=endpoint_context.netbox_id,
            vmid=payload.vmid,
            op="create",
            kind="lxc",
            status="failed",
            message="ostemplate required for LXC create",
        )

    raw_payload = payload.model_dump(mode="python")
    safe_payload = _scrub_lxc_payload(payload)
    try:
        logger.debug(
            "intent.apply: lxc create dispatching vmid=%s payload=%s",
            payload.vmid,
            safe_payload,
        )
        proxmox = await _open_proxmox_session(gated)
        params = _build_lxc_create_params(payload)
        response = await resolve_async(proxmox.session.nodes(payload.node).lxc.post(**params))
        upid = extract_upid(response)
    except Exception as error:  # noqa: BLE001
        message = scrub_message(str(error), raw_payload)
        logger.warning(
            "intent.apply: lxc create failed vmid=%s node=%s error=%s",
            payload.vmid,
            payload.node,
            message,
        )
        await write_intent_journal(
            journal_writer=write_verb_journal_entry,
            endpoint_context=endpoint_context,
            endpoint=gated,
            verb="intent_create_lxc",
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
            kind="lxc",
            status="failed",
            message=message,
        )

    await write_intent_journal(
        journal_writer=write_verb_journal_entry,
        endpoint_context=endpoint_context,
        endpoint=gated,
        verb="intent_create_lxc",
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
        kind="lxc",
        status="succeeded",
        proxmox_upid=upid,
    )
