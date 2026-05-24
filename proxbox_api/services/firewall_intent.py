"""Plan/apply helpers for firewall intent actions."""

from __future__ import annotations

import json
import time

from fastapi.responses import JSONResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import FirewallIntentRequestRecord
from proxbox_api.routes.intent.dispatchers.common import IntentEndpointContext
from proxbox_api.routes.intent.schemas import (
    ApplyDiff,
    ApplyResultItem,
    FirewallIntentPayload,
    IntentDiff,
    PlanVerdict,
)
from proxbox_api.routes.proxmox.firewall import _firewall_write


def is_firewall_plan_diff(diff: IntentDiff) -> bool:
    """Return True when an intent plan diff is a firewall mutation."""
    action = getattr(diff, "type", None) or getattr(diff, "action", None)
    return diff.kind == "firewall" or str(action or "").startswith("firewall.")


def plan_firewall_diff(diff: IntentDiff) -> PlanVerdict:
    """Return a plan verdict for a firewall mutation."""
    action = getattr(diff, "type", None) or getattr(diff, "action", None)
    return PlanVerdict(
        netbox_id=diff.netbox_id,
        op=diff.op,
        verdict="warning",
        reason="firewall_plan_preview",
        message=(
            f"Firewall action {action or diff.op!r} will be re-read and "
            "write-gated during /intent/apply."
        ),
    )


def _path_for(payload: FirewallIntentPayload) -> tuple[str, str, bool, str]:  # noqa: C901
    """Map a firewall intent payload to method/path/probe/reason."""
    action = payload.action
    zone = payload.zone or "datacenter"

    if action == "firewall.group.create":
        return "post", "cluster/firewall/groups", False, "firewall_write_not_supported"
    if action == "firewall.group.delete":
        if not payload.group:
            raise ValueError("firewall.group.delete requires group")
        return "delete", f"cluster/firewall/groups/{payload.group}", False, "firewall_write_not_supported"

    if action.startswith("firewall.rule."):
        base = _rule_base(payload, zone)
        if action.endswith(".create"):
            return "post", base, zone == "vnet", _unsupported_reason(zone)
        if payload.pos is None:
            raise ValueError(f"{action} requires pos")
        method = "put" if action.endswith(".update") else "delete"
        return method, f"{base}/{payload.pos}", zone == "vnet", _unsupported_reason(zone)

    if action.startswith("firewall.ipset."):
        base = _scoped_base(payload, zone, "ipset")
        if action.endswith(".create"):
            return "post", base, False, "firewall_write_not_supported"
        if not payload.name:
            raise ValueError(f"{action} requires name")
        return "delete", f"{base}/{payload.name}", False, "firewall_write_not_supported"

    if action == "firewall.alias.upsert":
        if not payload.name:
            raise ValueError("firewall.alias.upsert requires name")
        return "put", f"{_scoped_base(payload, zone, 'aliases')}/{payload.name}", False, (
            "firewall_write_not_supported"
        )

    if action == "firewall.options.update":
        return "put", _scoped_base(payload, zone, "options"), False, "firewall_write_not_supported"

    raise ValueError(f"Unsupported firewall intent action: {action}")


def _rule_base(payload: FirewallIntentPayload, zone: str) -> str:
    if zone == "datacenter":
        return "cluster/firewall/rules"
    if zone == "security_group":
        if not payload.group:
            raise ValueError("security_group rule intent requires group")
        return f"cluster/firewall/groups/{payload.group}"
    if zone == "node":
        if not payload.node:
            raise ValueError("node rule intent requires node")
        return f"nodes/{payload.node}/firewall/rules"
    if zone in {"vm_qemu", "vm_lxc"}:
        return f"{_vm_base(payload, zone)}/rules"
    if zone == "vnet":
        if not payload.vnet:
            raise ValueError("vnet rule intent requires vnet")
        return f"cluster/sdn/vnets/{payload.vnet}/firewall/rules"
    raise ValueError(f"Unsupported firewall rule zone: {zone}")


def _scoped_base(payload: FirewallIntentPayload, zone: str, resource: str) -> str:
    if zone == "datacenter":
        return f"cluster/firewall/{resource}"
    if zone in {"vm_qemu", "vm_lxc"}:
        return f"{_vm_base(payload, zone)}/{resource}"
    raise ValueError(f"{resource} is not supported for firewall zone {zone}")


def _vm_base(payload: FirewallIntentPayload, zone: str) -> str:
    if not payload.node or payload.vmid is None:
        raise ValueError(f"{zone} firewall intent requires node and vmid")
    vm_type = payload.vm_type or ("qemu" if zone == "vm_qemu" else "lxc")
    return f"nodes/{payload.node}/{vm_type}/{payload.vmid}/firewall"


def _unsupported_reason(zone: str) -> str:
    return "vnet_firewall_not_supported" if zone == "vnet" else "firewall_write_not_supported"


async def apply_firewall_diff(
    *,
    diff: ApplyDiff,
    endpoint_context: IntentEndpointContext,
    actor: str | None,
    run_uuid: str,
) -> ApplyResultItem:
    """Apply one firewall intent diff through the firewall write route helper."""
    del run_uuid
    if not isinstance(diff.payload, FirewallIntentPayload):
        return ApplyResultItem(
            netbox_id=diff.netbox_id,
            vmid=0,
            op=diff.op,
            kind=diff.kind,
            status="failed",
            reason="invalid_firewall_payload",
            message="firewall apply requires FirewallIntentPayload",
        )

    try:
        method, path, probe, unsupported_reason = _path_for(diff.payload)
        result = await _firewall_write(
            db=endpoint_context.session,
            endpoint_id=endpoint_context.endpoint_id,
            actor=actor,
            method=method,
            path=path,
            payload=diff.payload.body if method != "delete" else None,
            probe_supported=probe,
            unsupported_reason=unsupported_reason,
        )
        if isinstance(result, JSONResponse):
            body = json.loads(result.body.decode("utf-8"))
            return ApplyResultItem(
                netbox_id=diff.netbox_id,
                vmid=diff.payload.vmid or 0,
                op=diff.op,
                kind=diff.kind,
                status="failed",
                reason=str(body.get("reason") or "firewall_apply_failed"),
                message=str(body.get("detail") or body),
            )

        await _record_firewall_intent(
            session=endpoint_context.session,
            endpoint_id=endpoint_context.endpoint_id or 0,
            actor=actor,
            payload=diff.payload,
            state=result.status,
        )
        status = "skipped" if result.status == "skipped" else "succeeded"
        return ApplyResultItem(
            netbox_id=diff.netbox_id,
            vmid=diff.payload.vmid or 0,
            op=diff.op,
            kind=diff.kind,
            status=status,
            reason=result.reason,
            message=result.detail or f"{diff.payload.action} {result.status}",
            proxmox_upid=result.proxmox_task_id,
        )
    except Exception as error:  # noqa: BLE001
        return ApplyResultItem(
            netbox_id=diff.netbox_id,
            vmid=getattr(diff.payload, "vmid", None) or 0,
            op=diff.op,
            kind=diff.kind,
            status="failed",
            reason="firewall_apply_failed",
            message=str(error),
        )


async def _record_firewall_intent(
    *,
    session: object,
    endpoint_id: int,
    actor: str | None,
    payload: FirewallIntentPayload,
    state: str,
) -> None:
    if not isinstance(session, AsyncSession):
        return
    record = FirewallIntentRequestRecord(
        endpoint_id=endpoint_id,
        actor=actor,
        action=payload.action,
        state=state,
        payload=payload.model_dump(mode="python"),
        plan_snapshot={"recorded_at": time.time()},
    )
    session.add(record)
    await session.commit()
