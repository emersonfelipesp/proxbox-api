"""UPDATE dispatcher for QEMU intent diffs."""

from __future__ import annotations

from fastapi.responses import JSONResponse

from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.routes.intent.dispatchers.common import (
    coerce_endpoint_context,
    extract_upid,
    find_current_node,
    find_vmid_record,
    mapping_from_response,
    merge_indexed_delta,
    scrub_message,
    scrub_value,
    sequence_from_response,
    set_delta_if_changed,
    status_is_running,
    tags_to_config,
    write_intent_journal,
)
from proxbox_api.routes.intent.schemas import ApplyResultItem, VMIntentPayload
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.services.verb_dispatch import write_verb_journal_entry

QEMU_OFFLINE_REQUIRED_KEYS = {"cores", "memory"}
QEMU_OFFLINE_REQUIRED_PREFIXES = ("scsi", "virtio", "ide", "sata", "efidisk")


def _build_qemu_update_delta(
    payload: VMIntentPayload,
    current: dict[str, object],
) -> dict[str, object]:
    delta: dict[str, object] = {}
    fields_set = payload.model_fields_set

    if "name" in fields_set and payload.name is not None:
        set_delta_if_changed(delta, current, "name", payload.name)
    if "cores" in fields_set and payload.cores is not None:
        set_delta_if_changed(delta, current, "cores", payload.cores)
    if "memory_mib" in fields_set and payload.memory_mib is not None:
        set_delta_if_changed(delta, current, "memory", payload.memory_mib)
    if "tags" in fields_set:
        set_delta_if_changed(delta, current, "tags", tags_to_config(payload.tags))
    if "disks" in fields_set:
        merge_indexed_delta(delta, current, payload.disks, default_prefix="scsi")
    if "nics" in fields_set:
        merge_indexed_delta(delta, current, payload.nics, default_prefix="net")

    return delta


def _requires_vm_stop(delta: dict[str, object]) -> bool:
    for key in delta:
        if key in QEMU_OFFLINE_REQUIRED_KEYS or key.startswith(QEMU_OFFLINE_REQUIRED_PREFIXES):
            return True
    return False


async def _fail(
    *,
    endpoint_context,
    endpoint,
    payload: VMIntentPayload,
    actor: str | None,
    run_uuid: str,
    message: str,
    reason: str | None = None,
) -> ApplyResultItem:
    await write_intent_journal(
        journal_writer=write_verb_journal_entry,
        endpoint_context=endpoint_context,
        endpoint=endpoint,
        verb="intent_update_qemu",
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
        op="update",
        kind="qemu",
        status="failed",
        reason=reason,
        message=message,
    )


async def dispatch_qemu_update(
    endpoint,
    payload: VMIntentPayload,
    run_uuid: str,
    *,
    actor: str | None = None,
) -> ApplyResultItem:
    endpoint_context = coerce_endpoint_context(endpoint)
    gated = await _gate(endpoint_context.session, endpoint_context.endpoint_id)
    if isinstance(gated, JSONResponse):
        return await _fail(
            endpoint_context=endpoint_context,
            endpoint=None,
            payload=payload,
            actor=actor,
            run_uuid=run_uuid,
            message="writes_disabled_for_endpoint",
            reason="writes_disabled_for_endpoint",
        )

    raw_payload = payload.model_dump(mode="python")
    safe_payload = scrub_value(raw_payload)
    try:
        logger.debug(
            "intent.apply: qemu update dispatching vmid=%s payload=%s",
            payload.vmid,
            safe_payload,
        )
        proxmox = await _open_proxmox_session(gated)

        node_vms = sequence_from_response(
            await resolve_async(proxmox.session.nodes(payload.node).qemu.get())
        )
        if find_vmid_record(node_vms, vmid=payload.vmid, kind="qemu") is None:
            cluster_vms = sequence_from_response(
                await resolve_async(proxmox.session("cluster/resources").get(type="vm"))
            )
            current_node = find_current_node(cluster_vms, vmid=payload.vmid, kind="qemu")
            message = (
                f"TOCTOU mismatch: vmid {payload.vmid} was on node '{payload.node}' "
                f"at plan, now on '{current_node}'"
            )
            return await _fail(
                endpoint_context=endpoint_context,
                endpoint=gated,
                payload=payload,
                actor=actor,
                run_uuid=run_uuid,
                message=message,
                reason="toctou_mismatch",
            )

        current_config = mapping_from_response(
            await resolve_async(
                proxmox.session.nodes(payload.node).qemu(payload.vmid).config.get()
            )
        )
        delta = _build_qemu_update_delta(payload, current_config)
        if not delta:
            await write_intent_journal(
                journal_writer=write_verb_journal_entry,
                endpoint_context=endpoint_context,
                endpoint=gated,
                verb="intent_update_qemu",
                result="skipped",
                vmid=payload.vmid,
                actor=actor,
                run_uuid=run_uuid,
                kind="info",
            )
            return ApplyResultItem(
                netbox_id=endpoint_context.netbox_id,
                vmid=payload.vmid,
                op="update",
                kind="qemu",
                status="skipped",
                message="No QEMU update needed; current Proxmox config already matches payload",
            )

        current_status = await resolve_async(
            proxmox.session.nodes(payload.node).qemu(payload.vmid).status.current.get()
        )
        if _requires_vm_stop(delta) and status_is_running(current_status):
            message = f"Update requires VM stop; refusing to auto-stop running VM {payload.vmid}"
            return await _fail(
                endpoint_context=endpoint_context,
                endpoint=gated,
                payload=payload,
                actor=actor,
                run_uuid=run_uuid,
                message=message,
                reason="update_requires_vm_stop",
            )

        response = await resolve_async(
            proxmox.session.nodes(payload.node).qemu(payload.vmid).config.put(**delta)
        )
        upid = extract_upid(response)
    except Exception as error:  # noqa: BLE001
        message = scrub_message(str(error), raw_payload)
        logger.warning(
            "intent.apply: qemu update failed vmid=%s node=%s error=%s",
            payload.vmid,
            payload.node,
            message,
        )
        return await _fail(
            endpoint_context=endpoint_context,
            endpoint=gated,
            payload=payload,
            actor=actor,
            run_uuid=run_uuid,
            message=message,
        )

    await write_intent_journal(
        journal_writer=write_verb_journal_entry,
        endpoint_context=endpoint_context,
        endpoint=gated,
        verb="intent_update_qemu",
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
        op="update",
        kind="qemu",
        status="succeeded",
        proxmox_upid=upid,
        message=f"Updated QEMU VM {payload.vmid}: {', '.join(sorted(delta))}",
    )
