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

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    "pool:noop": True,
    "flag:noop": True,
    "osd:noop": True,
    "mon:noop": True,
    "mgr:noop": True,
    "mds:noop": True,
    "filesystem:noop": True,
    "crush_rule:noop": True,
    # Not yet exposed by PVE CephWrite — reported, never silently dropped.
    "filesystem:update": False,
    "filesystem:delete": False,
    "crush_rule:create": False,
    "crush_rule:update": False,
    "crush_rule:delete": False,
}

# These exact proxmox-sdk helpers are typed and tested as synchronous: a
# successful await returns ``None`` only after PVE accepted and completed the
# mutation.  No other operation may infer success from a missing task UPID.
SYNCHRONOUS_OPERATION_KINDS = frozenset(
    {
        "flag:create",
        "flag:update",
        "flag:delete",
        "osd:update",
    }
)


class _Payload(BaseModel):
    """Strict canonical payload base used by both planning and dispatch."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _EmptyPayload(_Payload):
    pass


class _PoolCreatePayload(_Payload):
    add_storages: bool | None = None
    application: str | None = None
    crush_rule: str | None = None
    erasure_coding: str | None = None
    min_size: int | None = Field(default=None, ge=1)
    pg_autoscale_mode: str | None = None
    pg_num: int | None = Field(default=None, ge=1)
    pg_num_min: int | None = Field(default=None, ge=1)
    size: int | None = Field(default=None, ge=1)
    target_size: str | None = None
    target_size_ratio: float | None = Field(default=None, gt=0)


class _PoolUpdatePayload(_Payload):
    application: str | None = None
    crush_rule: str | None = None
    min_size: int | None = Field(default=None, ge=1)
    pg_autoscale_mode: str | None = None
    pg_num: int | None = Field(default=None, ge=1)
    pg_num_min: int | None = Field(default=None, ge=1)
    size: int | None = Field(default=None, ge=1)
    target_size: str | None = None
    target_size_ratio: float | None = Field(default=None, gt=0)


class _PoolDeletePayload(_Payload):
    force: bool | None = None
    remove_ecprofile: bool | None = None
    remove_storages: bool | None = None


class _OSDCreatePayload(_Payload):
    dev: str = Field(min_length=1)
    crush_device_class: str | None = None
    db_dev: str | None = None
    db_dev_size: float | None = Field(default=None, gt=0)
    encrypted: bool | None = None
    osds_per_device: int | None = Field(default=None, ge=1)
    wal_dev: str | None = None
    wal_dev_size: float | None = Field(default=None, gt=0)


class _OSDUpdatePayload(_Payload):
    in_state: bool = Field(alias="in")


class _OSDDeletePayload(_Payload):
    cleanup: bool | None = None


class _MonCreatePayload(_Payload):
    mon_address: str | None = None


class _MDSCreatePayload(_Payload):
    hotstandby: bool | None = None


class _FilesystemCreatePayload(_Payload):
    add_storage: bool | None = None
    pg_num: int | None = Field(default=None, ge=1)


_PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    "pool:create": _PoolCreatePayload,
    "pool:update": _PoolUpdatePayload,
    "pool:delete": _PoolDeletePayload,
    "flag:create": _EmptyPayload,
    "flag:update": _EmptyPayload,
    "flag:delete": _EmptyPayload,
    "osd:create": _OSDCreatePayload,
    "osd:update": _OSDUpdatePayload,
    "osd:delete": _OSDDeletePayload,
    "mon:create": _MonCreatePayload,
    "mon:delete": _EmptyPayload,
    "mgr:create": _EmptyPayload,
    "mgr:delete": _EmptyPayload,
    "mds:create": _MDSCreatePayload,
    "mds:delete": _EmptyPayload,
    "filesystem:create": _FilesystemCreatePayload,
}


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
        key: bool(supported and (writes_enabled or key.endswith(":noop")))
        for key, supported in WRITE_OPERATION_KINDS.items()
    }


def resolve_node(operation: ProviderOperation, node_names: list[str]) -> str:
    """Require the exact node persisted in the canonical operation plan."""

    node = operation.node
    if not node:
        raise CephCapabilityUnsupported(
            "A Proxmox Ceph mutation requires an exact node bound in the plan."
        )
    known_nodes = {str(item) for item in node_names if str(item)}
    if not known_nodes or node not in known_nodes:
        raise CephCapabilityUnsupported(
            "The node bound in the Ceph plan is not present in the selected endpoint."
        )
    return node


def validate_operation_payload(operation: ProviderOperation) -> dict[str, Any]:
    """Validate and canonicalize the exact payload for one mutation pair.

    Unknown keys and missing required fields are rejected here during planning
    and again at the provider sink. No SDK-signature filtering is allowed.
    """

    if operation.action == "noop":
        return dict(operation.after_summary)
    op_key = f"{operation.kind}:{operation.action}"
    model = _PAYLOAD_MODELS.get(op_key)
    if model is None:
        raise _gap(operation.kind, operation.action)
    try:
        validated = model.model_validate(operation.after_summary or {})
    except ValidationError:
        raise CephCapabilityUnsupported(
            f"Proxmox Ceph write payload is invalid for {op_key}."
        ) from None
    return validated.model_dump(mode="python", by_alias=True, exclude_none=True)


def _result(
    operation: ProviderOperation,
    upid: Any,
    result: str,
    *,
    completion_mode: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "operation_id": operation.id,
        "result": result,
        "target_ref": operation.target_ref,
        "action": operation.action,
        "kind": operation.kind,
    }
    if upid is not None:
        out["upid"] = upid if isinstance(upid, str) else str(upid)
    if completion_mode is not None:
        out["completion_mode"] = completion_mode
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
    op_key = f"{kind}:{action}"
    if WRITE_OPERATION_KINDS.get(op_key) is not True:
        raise _gap(kind, action)
    if action == "noop":
        return _result(operation, None, "noop")
    if operation.node != node:
        raise CephCapabilityUnsupported(
            "The dispatch node does not match the immutable Ceph plan binding."
        )
    payload = validate_operation_payload(operation)

    provider_result = await _dispatch(
        write,
        kind,
        action,
        node,
        target,
        payload,
        confirm_destructive,
    )
    if op_key in SYNCHRONOUS_OPERATION_KINDS and provider_result is None:
        return _result(
            operation,
            None,
            "completed",
            completion_mode="synchronous",
        )
    return _result(operation, provider_result, "submitted")


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
            return await write.pool_create(node, target, **payload)
        if action == "update":
            return await write.pool_set(node, target, **payload)
        if action == "delete":
            return await write.pool_delete(node, target, confirm_destroy=confirm, **payload)
    elif kind == "flag":
        if action in ("create", "update"):
            return await write.flag_set(target)
        if action == "delete":
            return await write.flag_unset(target)
    elif kind == "osd":
        if action == "create":
            dev = payload.get("dev")
            assert isinstance(dev, str)  # nosec B101 - guaranteed by typed payload validation
            rest = {key: value for key, value in payload.items() if key != "dev"}
            return await write.osd_create(node, dev, **rest)
        if action == "delete":
            return await write.osd_delete(node, target, confirm_destroy=confirm, **payload)
        if action == "update":
            state = payload.get("in")
            if state is True:
                return await write.osd_in(node, target)
            if state is False:
                return await write.osd_out(node, target)
            raise _gap(kind, action)
    elif kind == "mon":
        if action == "create":
            return await write.mon_create(node, target, **payload)
        if action == "delete":
            return await write.mon_delete(node, target, confirm_destroy=confirm)
    elif kind == "mgr":
        if action == "create":
            return await write.mgr_create(node, target)
        if action == "delete":
            return await write.mgr_delete(node, target, confirm_destroy=confirm)
    elif kind == "mds":
        if action == "create":
            return await write.mds_create(node, target, **payload)
        if action == "delete":
            return await write.mds_delete(node, target, confirm_destroy=confirm)
    elif kind == "filesystem":
        if action == "create":
            return await write.cephfs_create(node, target, **payload)
    raise _gap(kind, action)
