"""Shared helpers for Proxmox VMID and VM type extraction from NetBox VM payloads."""

from __future__ import annotations

def normalize_vmid(vmid: object) -> str | None:
    """Normalize VMID values for safe cross-system comparisons."""
    if vmid is None:
        return None
    vmid_str = str(vmid).strip()
    return vmid_str or None


def extract_proxmox_vmid(vm: dict[str, object]) -> str | None:
    """Extract Proxmox VMID from NetBox VM payload across known field layouts.

    Handles both RestRecord objects and plain dicts by detecting the interface
    and converting as needed.
    """
    if hasattr(vm, "get"):
        vm = vm.dict() if hasattr(vm, "dict") else dict(vm)
    else:
        vm = dict(vm) if vm else {}

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


def extract_proxmox_vm_type(vm: dict[str, object]) -> str | None:
    """Extract Proxmox VM type from NetBox VM payload across known field layouts.

    Returns 'qemu' or 'lxc' (case-normalized), or None if not found.

    Handles both RestRecord objects and plain dicts by detecting the interface
    and converting as needed.
    """
    if hasattr(vm, "get"):
        vm = vm.dict() if hasattr(vm, "dict") else dict(vm)
    else:
        vm = dict(vm) if vm else {}

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
