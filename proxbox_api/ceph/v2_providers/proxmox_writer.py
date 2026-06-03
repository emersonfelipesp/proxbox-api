"""Map Ceph v2 ProviderOperations to ``proxmox_sdk.ceph.CephWrite`` calls.

The Ceph v2 plan/apply engine (see ``proxbox_api/ceph/v2_engine.py``) hands the
provider adapter one :class:`ProviderOperation` at a time and harvests a Proxmox
task UPID from the returned dict.  This module owns the deterministic mapping
from ``(kind, action)`` to a guarded ``CephWrite`` helper so the
``ProxmoxCephProviderAdapter`` stays small and the mapping is unit-testable with
an injected fake.

Writes are only available when the installed ``proxmox-sdk`` ships the
``CephWrite`` domain.  When it does not (older pin), the adapter reports
``apply=False`` via :func:`cephwrite_importable` and never silently no-ops a
real write request.
"""

from __future__ import annotations

import inspect
from typing import Any

from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported
from proxbox_api.ceph.v2_schemas import ProviderOperation

# Capability map: ``operation_kind`` -> supported by the Proxmox write adapter.
# ``True`` entries light up once ``CephWrite`` is importable; ``False`` entries
# are surfaced as explicitly-unsupported so the plan engine never hides a gap.
WRITE_OPERATION_KINDS: dict[str, bool] = {
    "pool:create": True,
    "pool:update": True,
    "pool:delete": True,
    "flag:create": True,
    "flag:update": True,
    "flag:delete": True,
    "osd:create": True,
    "osd:delete": True,
    "osd:update": True,
    "mon:create": True,
    "mon:delete": True,
    "mgr:create": True,
    "mgr:delete": True,
    "mds:create": True,
    "mds:delete": True,
    "filesystem:create": True,
    "noop": True,
    # Not yet exposed by PVE CephWrite — reported, never silently dropped.
    "filesystem:update": False,
    "filesystem:delete": False,
    "crush_rule:create": False,
    "crush_rule:update": False,
    "crush_rule:delete": False,
}

# Control keys consumed by the executor itself, never forwarded as write params.
_CONTROL_KEYS = frozenset({"node", "name", "target_ref", "confirm_destroy", "confirm_destructive"})


def cephwrite_importable() -> bool:
    """Return ``True`` when the installed ``proxmox-sdk`` exposes ``CephWrite``."""
    try:
        from proxmox_sdk.ceph import CephWrite  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001 - any import failure means writes unavailable
        return False
    return True


def operation_kinds(writes_enabled: bool) -> dict[str, bool]:
    """Resolve the advertised ``operation_kinds`` map given write availability."""
    return {
        key: bool(supported and writes_enabled) for key, supported in WRITE_OPERATION_KINDS.items()
    }


def resolve_node(operation: ProviderOperation, node_names: list[str]) -> str:
    """Pick the Proxmox node for a write: explicit payload node, else first node."""
    node = None
    if isinstance(operation.after_summary, dict):
        node = operation.after_summary.get("node")
    if not node and node_names:
        node = node_names[0]
    if not node:
        raise CephCapabilityUnsupported(
            "No Proxmox node available to apply the Ceph write operation."
        )
    return str(node)


def _params(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (payload or {}).items()
        if key not in _CONTROL_KEYS and value is not None
    }


def _filtered(method: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only kwargs the bound ``CephWrite`` method accepts (or all if **kwargs)."""
    sig = inspect.signature(method)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return dict(payload)
    accepted = {
        name
        for name, p in sig.parameters.items()
        if p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    }
    return {key: value for key, value in payload.items() if key in accepted}


def _result(operation: ProviderOperation, upid: Any, result: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "operation_id": operation.id,
        "result": result,
        "target_ref": operation.target_ref,
        "action": operation.action,
        "kind": operation.kind,
    }
    if upid is not None:
        out["upid"] = upid if isinstance(upid, str) else str(upid)
    return out


def _gap(kind: str, action: str, detail: str | None = None) -> CephCapabilityUnsupported:
    suffix = f" {detail}" if detail else ""
    return CephCapabilityUnsupported(
        f"Proxmox Ceph write adapter does not support {kind}:{action}.{suffix}"
    )


async def execute_operation(
    write: Any,
    operation: ProviderOperation,
    node: str,
    *,
    confirm_destructive: bool,
) -> dict[str, Any]:
    """Execute one planned operation through ``CephWrite`` and return a result dict.

    The returned dict carries ``upid`` (when the provider yields a task id) which
    the engine harvests into the operation run's ``provider_task_refs``.
    """
    kind = operation.kind
    action = operation.action
    target = operation.target_ref or ""
    payload = _params(operation.after_summary)
    op_key = "noop" if action == "noop" else f"{kind}:{action}"

    if WRITE_OPERATION_KINDS.get(op_key) is False:
        raise _gap(kind, action)

    if action == "noop":
        return _result(operation, None, "noop")

    upid = await _dispatch(write, kind, action, node, target, payload, confirm_destructive)
    return _result(operation, upid, "applied")


async def _dispatch(  # noqa: C901, PLR0911, PLR0912 - explicit kind/action table
    write: Any,
    kind: str,
    action: str,
    node: str,
    target: str,
    payload: dict[str, Any],
    confirm: bool,
) -> Any:
    if kind == "pool":
        if action == "create":
            return await write.pool_create(node, target, **_filtered(write.pool_create, payload))
        if action == "update":
            return await write.pool_set(node, target, **_filtered(write.pool_set, payload))
        if action == "delete":
            return await write.pool_delete(
                node, target, confirm_destroy=confirm, **_filtered(write.pool_delete, payload)
            )
    elif kind == "flag":
        if action in ("create", "update"):
            return await write.flag_set(target)
        if action == "delete":
            return await write.flag_unset(target)
    elif kind == "osd":
        if action == "create":
            dev = payload.get("dev")
            if not dev:
                raise _gap(kind, action, "osd create requires payload 'dev' (device path).")
            rest = {key: value for key, value in payload.items() if key != "dev"}
            return await write.osd_create(node, dev, **_filtered(write.osd_create, rest))
        if action == "delete":
            return await write.osd_delete(
                node, target, confirm_destroy=confirm, **_filtered(write.osd_delete, payload)
            )
        if action == "update":
            state = payload.get("in")
            if state is True:
                return await write.osd_in(node, target)
            if state is False:
                return await write.osd_out(node, target)
            raise _gap(kind, action, "osd update requires payload {'in': true|false}.")
    elif kind == "mon":
        if action == "create":
            return await write.mon_create(node, target, **_filtered(write.mon_create, payload))
        if action == "delete":
            return await write.mon_delete(node, target, confirm_destroy=confirm)
    elif kind == "mgr":
        if action == "create":
            return await write.mgr_create(node, target)
        if action == "delete":
            return await write.mgr_delete(node, target, confirm_destroy=confirm)
    elif kind == "mds":
        if action == "create":
            return await write.mds_create(node, target, **_filtered(write.mds_create, payload))
        if action == "delete":
            return await write.mds_delete(node, target, confirm_destroy=confirm)
    elif kind == "filesystem":
        if action == "create":
            return await write.cephfs_create(
                node, target, **_filtered(write.cephfs_create, payload)
            )
    raise _gap(kind, action)
