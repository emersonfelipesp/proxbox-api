"""Shared helpers for Proxmox VM identity extraction from typed sync state."""

from __future__ import annotations

from proxbox_api.logger import logger


def _coerce_mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "serialize"):
        try:
            serialized = value.serialize()
            if isinstance(serialized, dict):
                return serialized
        except Exception:
            return {}
    if hasattr(value, "dict"):
        try:
            dumped = value.dict()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            return {}
    try:
        return dict(value) if value else {}
    except Exception:
        return {}


def normalize_vmid(vmid: object) -> str | None:
    """Normalize VMID values for safe cross-system comparisons."""
    if vmid is None:
        return None
    vmid_str = str(vmid).strip()
    return vmid_str or None


def normalize_positive_int(value: object) -> int | None:
    """Normalize a positive integer identity value from mixed API payload shapes."""
    normalized = normalize_vmid(value)
    if normalized is None:
        return None
    try:
        result = int(normalized)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def extract_proxmox_vmid(vm: dict[str, object]) -> str | None:
    """Extract Proxmox VMID from a typed sync-state mapping.

    Handles both RestRecord objects and plain dicts by detecting the interface
    and converting as needed.
    """
    vm = _coerce_mapping(vm)

    for key in ("proxmox_vm_id", "proxmox_vmid"):
        normalized = normalize_vmid(vm.get(key))
        if normalized:
            return normalized
    return None


def extract_proxmox_endpoint_id(vm: dict[str, object]) -> int | None:
    """Extract the Proxmox endpoint identity stored on a NetBox VM record."""
    vm = _coerce_mapping(vm)

    for key in ("proxmox_endpoint_raw_id", "proxmox_endpoint_id"):
        endpoint_id = normalize_positive_int(vm.get(key))
        if endpoint_id is not None:
            return endpoint_id
    return None


def extract_proxmox_node(vm: dict[str, object]) -> str | None:
    """Extract the stored Proxmox node name from a NetBox VM record."""
    vm = _coerce_mapping(vm)

    for key in ("proxmox_node_name", "proxmox_node"):
        value = normalize_vmid(vm.get(key))
        if value:
            return value
    return None


def extract_proxmox_session_endpoint_id(session: object) -> int | None:
    """Extract the endpoint id attached to a Proxmox session object."""
    for attr_name in ("db_endpoint_id", "endpoint_id", "proxmox_endpoint_id"):
        endpoint_id = normalize_positive_int(getattr(session, attr_name, None))
        if endpoint_id is not None:
            return endpoint_id
    return None


def select_proxmox_sessions_by_endpoint(
    sessions: object,
    endpoint_id: int | None,
) -> list[object]:
    """Return only sessions that belong to ``endpoint_id`` when it is known."""
    session_list = list(sessions or [])
    if endpoint_id is None:
        return session_list
    return [
        session
        for session in session_list
        if extract_proxmox_session_endpoint_id(session) == endpoint_id
    ]


def scope_vms_to_proxmox_sessions(
    vms: list[dict[str, object]],
    sessions: object,
    *,
    stage: str,
) -> list[dict[str, object]]:
    """Keep the VMs that the Proxmox sessions of this run can own.

    An estate run lists every NetBox VM. When the caller selected one Proxmox
    endpoint, the VMs of all other endpoints cannot be read through the selected
    sessions: each one costs task setup, progress messages and a skip warning,
    and a multi-endpoint run repeats that work once per endpoint.

    A VM is dropped only on proof that it is out of scope: its sync-state
    endpoint id is known, every session exposes an endpoint id, and none of
    them matches. A VM without an endpoint id, and a run in which a session
    has no endpoint id, keep the previous unscoped behaviour.
    """
    session_list = list(sessions or [])
    session_endpoint_ids = {
        extract_proxmox_session_endpoint_id(session) for session in session_list
    }
    if not session_list or None in session_endpoint_ids:
        return vms

    in_scope: list[dict[str, object]] = []
    out_of_scope_endpoint_ids: set[int] = set()
    for vm in vms:
        endpoint_id = extract_proxmox_endpoint_id(vm)
        if endpoint_id is None or endpoint_id in session_endpoint_ids:
            in_scope.append(vm)
        else:
            out_of_scope_endpoint_ids.add(endpoint_id)

    dropped = len(vms) - len(in_scope)
    if dropped:
        logger.info(
            "%s sync: %d VM(s) of %d other Proxmox endpoint id(s) are outside the "
            "selected endpoint id(s) %s and were not processed",
            stage,
            dropped,
            len(out_of_scope_endpoint_ids),
            sorted(session_endpoint_ids),
        )
    return in_scope


def extract_proxmox_vm_type(vm: dict[str, object]) -> str | None:
    """Extract Proxmox VM type from a typed sync-state mapping.

    Returns 'qemu' or 'lxc' (case-normalized), or None if not found.

    Handles both RestRecord objects and plain dicts by detecting the interface
    and converting as needed.
    """
    vm = _coerce_mapping(vm)

    for key in ("proxmox_vm_type",):
        value = vm.get(key)
        if value and str(value).strip().lower() in ("qemu", "lxc"):
            return str(value).strip().lower()
    return None
