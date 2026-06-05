"""Map Ceph v2 ProviderOperations to Ceph Dashboard client calls (#98).

Mirrors ``proxmox_writer`` for the direct Ceph Dashboard provider: a
deterministic ``(kind, action) -> DashboardCephClient`` table so the
``DashboardCephProviderAdapter`` stays thin and the mapping is unit-testable
with an injected fake client.

The Dashboard API exposes pool and RBD (block image / snapshot) writes; RGW and
CRUSH writes are reported as unsupported here (they belong to the RGW Admin Ops
and external providers), never silently dropped.
"""

from __future__ import annotations

from typing import Any

from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported
from proxbox_api.ceph.v2_schemas import ProviderOperation

# Capability map for the Dashboard write adapter. ``True`` lights up once the
# SDK Dashboard client is importable; ``False`` is surfaced as an explicit gap.
WRITE_OPERATION_KINDS: dict[str, bool] = {
    "pool:create": True,
    "pool:update": True,
    "pool:delete": True,
    "rbd_image:create": True,
    "rbd_image:update": True,
    "rbd_image:delete": True,
    "rbd_image:trash": True,
    "rbd_snapshot:create": True,
    "rbd_snapshot:delete": True,
    "noop": True,
    # Reported unsupported (handled by rgw_admin / external providers).
    "rgw_bucket:create": False,
    "rgw_bucket:delete": False,
    "rgw_user:create": False,
    "rgw_user:delete": False,
    "crush_rule:create": False,
    "crush_rule:update": False,
    "crush_rule:delete": False,
}

_CONTROL_KEYS = frozenset({"node", "confirm_destroy", "confirm_destructive"})


def operation_kinds(writes_enabled: bool) -> dict[str, bool]:
    """Resolve the advertised ``operation_kinds`` given Dashboard availability."""
    return {
        key: bool(supported and writes_enabled) for key, supported in WRITE_OPERATION_KINDS.items()
    }


def _payload(operation: ProviderOperation) -> dict[str, Any]:
    summary = operation.after_summary if isinstance(operation.after_summary, dict) else {}
    return {
        key: value
        for key, value in summary.items()
        if key not in _CONTROL_KEYS and value is not None
    }


def _image_spec(operation: ProviderOperation, payload: dict[str, Any]) -> str:
    """Build ``pool/image`` (optionally with namespace) from payload/target_ref."""
    pool = payload.get("pool_name") or payload.get("pool")
    name = payload.get("name") or payload.get("image")
    namespace = payload.get("namespace")
    if pool and name:
        parts = [str(pool)]
        if namespace:
            parts.append(str(namespace))
        parts.append(str(name))
        return "/".join(parts)
    # Fall back to target_ref, expected to already be "pool/image".
    return operation.target_ref or ""


def _result(operation: ProviderOperation, raw: Any, result: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "operation_id": operation.id,
        "result": result,
        "target_ref": operation.target_ref,
        "action": operation.action,
        "kind": operation.kind,
    }
    # Dashboard write calls may return a task descriptor; surface its name as a ref.
    if isinstance(raw, dict):
        task = raw.get("name") or raw.get("task") or raw.get("metadata")
        if isinstance(task, str):
            out["provider_task_ref"] = task
    return out


def _gap(kind: str, action: str, detail: str | None = None) -> CephCapabilityUnsupported:
    suffix = f" {detail}" if detail else ""
    return CephCapabilityUnsupported(
        f"Ceph Dashboard write adapter does not support {kind}:{action}.{suffix}"
    )


async def execute_dashboard_operation(
    client: Any,
    operation: ProviderOperation,
    *,
    confirm_destructive: bool,
) -> dict[str, Any]:
    """Execute one planned operation through the Dashboard client."""
    kind = operation.kind
    action = operation.action
    op_key = "noop" if action == "noop" else f"{kind}:{action}"

    if WRITE_OPERATION_KINDS.get(op_key) is False:
        raise _gap(kind, action)
    if action == "noop":
        return _result(operation, None, "noop")

    payload = _payload(operation)
    raw = await _dispatch(client, kind, action, operation, payload, confirm_destructive)
    return _result(operation, raw, "applied")


async def _dispatch(  # noqa: C901, PLR0911, PLR0912 - explicit kind/action table
    client: Any,
    kind: str,
    action: str,
    operation: ProviderOperation,
    payload: dict[str, Any],
    confirm: bool,
) -> Any:
    target = operation.target_ref or ""
    if kind == "pool":
        if action == "create":
            return await client.pool_create(payload or {"pool_name": target})
        if action == "update":
            return await client.pool_edit(target, payload)
        if action == "delete":
            return await client.pool_delete(target, confirm_destroy=confirm)
    elif kind == "rbd_image":
        spec = _image_spec(operation, payload)
        if action == "create":
            return await client.rbd_create(payload)
        if action == "update":
            return await client.rbd_edit(spec, payload)
        if action == "delete":
            return await client.rbd_delete(spec, confirm_destroy=confirm)
        if action == "trash":
            return await client.rbd_move_trash(spec, confirm_destroy=confirm)
    elif kind == "rbd_snapshot":
        spec = _image_spec(operation, payload)
        snap = payload.get("snapshot_name") or payload.get("name") or ""
        if not snap:
            raise _gap(kind, action, "snapshot requires payload 'snapshot_name'.")
        if action == "create":
            return await client.rbd_snapshot_create(spec, snap)
        if action == "delete":
            return await client.rbd_snapshot_delete(spec, snap, confirm_destroy=confirm)
    raise _gap(kind, action)
