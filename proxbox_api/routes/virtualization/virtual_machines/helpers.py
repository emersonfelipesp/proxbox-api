"""Concurrency helpers for virtual machine sync routes."""

import os


def resolve_vm_sync_concurrency() -> int:
    raw_value = os.environ.get("PROXBOX_VM_SYNC_MAX_CONCURRENCY", "").strip()
    if not raw_value:
        return 4
    try:
        value = int(raw_value)
    except ValueError:
        return 4
    return max(1, value)
