"""Shared helpers for Proxmox VM identity extraction from NetBox VM payloads."""

from __future__ import annotations


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
    """Extract Proxmox VMID from NetBox VM payload across known field layouts.

    Handles both RestRecord objects and plain dicts by detecting the interface
    and converting as needed.
    """
    vm = _coerce_mapping(vm)

    top_level_keys = (
        "cf_proxmox_vm_id",
        "proxmox_vm_id",
        "cf_proxmox_vmid",
        "proxmox_vmid",
    )
    for key in top_level_keys:
        normalized = normalize_vmid(vm.get(key))
        if normalized:
            return normalized

    custom_fields = vm.get("custom_fields")
    if isinstance(custom_fields, dict):
        custom_field_keys = (
            "proxmox_vm_id",
            "cf_proxmox_vm_id",
            "proxmox_vmid",
            "cf_proxmox_vmid",
        )
        for key in custom_field_keys:
            normalized = normalize_vmid(custom_fields.get(key))
            if normalized:
                return normalized
    return None


def extract_proxmox_endpoint_id(vm: dict[str, object]) -> int | None:
    """Extract the Proxmox endpoint identity stored on a NetBox VM record."""
    vm = _coerce_mapping(vm)

    for key in ("cf_proxmox_endpoint_id", "proxmox_endpoint_id"):
        endpoint_id = normalize_positive_int(vm.get(key))
        if endpoint_id is not None:
            return endpoint_id

    custom_fields = vm.get("custom_fields")
    if isinstance(custom_fields, dict):
        for key in ("proxmox_endpoint_id", "cf_proxmox_endpoint_id"):
            endpoint_id = normalize_positive_int(custom_fields.get(key))
            if endpoint_id is not None:
                return endpoint_id
    return None


def extract_proxmox_node(vm: dict[str, object]) -> str | None:
    """Extract the stored Proxmox node name from a NetBox VM record."""
    vm = _coerce_mapping(vm)

    for key in ("cf_proxmox_node", "proxmox_node"):
        value = normalize_vmid(vm.get(key))
        if value:
            return value

    custom_fields = vm.get("custom_fields")
    if isinstance(custom_fields, dict):
        for key in ("proxmox_node", "cf_proxmox_node"):
            value = normalize_vmid(custom_fields.get(key))
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


def extract_proxmox_vm_type(vm: dict[str, object]) -> str | None:
    """Extract Proxmox VM type from NetBox VM payload across known field layouts.

    Returns 'qemu' or 'lxc' (case-normalized), or None if not found.

    Handles both RestRecord objects and plain dicts by detecting the interface
    and converting as needed.
    """
    vm = _coerce_mapping(vm)

    top_level_keys = (
        "cf_proxmox_vm_type",
        "proxmox_vm_type",
    )
    for key in top_level_keys:
        value = vm.get(key)
        if value and str(value).strip().lower() in ("qemu", "lxc"):
            return str(value).strip().lower()

    custom_fields = vm.get("custom_fields")
    if isinstance(custom_fields, dict):
        for key in ("proxmox_vm_type", "cf_proxmox_vm_type"):
            value = custom_fields.get(key)
            if value and str(value).strip().lower() in ("qemu", "lxc"):
                return str(value).strip().lower()
    return None
