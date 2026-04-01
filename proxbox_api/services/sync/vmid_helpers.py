"""Shared helpers for Proxmox VMID extraction from NetBox VM payloads."""

from __future__ import annotations

from typing import Any


def normalize_vmid(vmid: Any) -> str | None:
    """Normalize VMID values for safe cross-system comparisons."""
    if vmid is None:
        return None
    vmid_str = str(vmid).strip()
    return vmid_str or None


def extract_proxmox_vmid(vm: dict[str, Any]) -> str | None:
    """Extract Proxmox VMID from NetBox VM payload across known field layouts."""
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