"""Regression tests for VM status reconciliation across a re-sync.

netbox-proxbox issue #617: "VM name and status do not change during
synchronization if they were changed in Proxmox." The reporter's VM was first
synced while it was a Proxmox template, then converted to a running VM — and
NetBox kept showing the old status.

``ProxmoxToNetBoxVMStatus.from_proxmox`` is applied to **both** sides of the
reconciliation diff: the Proxmox-derived desired status, and the existing NetBox
record's status (via ``normalize_current_virtual_machine_payload`` →
``NetBoxVirtualMachineCreateBody``'s validator). The existing record is loaded
over raw REST, where NetBox serialises a choice field as
``{"value": ..., "label": ...}`` rather than a bare string.

Before the fix, ``str({"value": "offline", ...}).strip().lower()`` matched no key
in the mapping and fell through to the ``active`` default. So *every* existing
record read back as ``active`` regardless of what was stored — and when the new
desired status was also ``active`` (exactly the template → running case), the
diff saw no change and emitted no patch.
"""

from __future__ import annotations

import pytest

from proxbox_api.enum.status_mapping import ProxmoxToNetBoxVMStatus
from proxbox_api.services.sync.vm_helpers import (
    normalize_current_virtual_machine_payload,
)


@pytest.mark.parametrize(
    ("netbox_choice", "expected"),
    [
        ({"value": "offline", "label": "Offline"}, "offline"),
        ({"value": "active", "label": "Active"}, "active"),
        ({"value": "planned", "label": "Planned"}, "planned"),
        # Defensive: a payload carrying only the label still resolves.
        ({"label": "Offline"}, "offline"),
    ],
)
def test_netbox_nested_choice_status_is_unwrapped(netbox_choice, expected):
    """NetBox's nested choice object must map to its real status, not the default."""
    assert ProxmoxToNetBoxVMStatus.from_proxmox(netbox_choice).value == expected


@pytest.mark.parametrize(
    ("proxmox_status", "expected"),
    [
        ("running", "active"),
        ("online", "active"),
        ("stopped", "offline"),
        ("paused", "offline"),
        ("planned", "planned"),
        (None, "active"),
        ("", "active"),
        ("something-unknown", "active"),
    ],
)
def test_plain_proxmox_status_strings_still_map(proxmox_status, expected):
    """The original bare-string behaviour is unchanged."""
    assert ProxmoxToNetBoxVMStatus.from_proxmox(proxmox_status).value == expected


def test_offline_vm_turning_active_produces_a_status_diff():
    """The reporter's scenario: an offline/template VM that is now running.

    Fails on the pre-fix code, where the existing record's nested ``offline``
    choice read back as ``active`` and the diff therefore saw no change.
    """
    existing_record = {
        "name": "node57-k8s",
        # As returned by NetBox's REST API for a choice field.
        "status": {"value": "offline", "label": "Offline"},
        "custom_fields": {"proxmox_vm_id": 254},
    }
    current = normalize_current_virtual_machine_payload(existing_record)
    current_status = ProxmoxToNetBoxVMStatus.from_proxmox(current["status"]).value

    desired_status = ProxmoxToNetBoxVMStatus.from_proxmox("running").value

    assert current_status == "offline", (
        "the existing NetBox status must survive normalization; reading it back "
        "as 'active' is what silently suppressed the status patch"
    )
    assert desired_status == "active"
    assert current_status != desired_status, (
        "an offline VM that is now running must produce a status diff so the "
        "reconciliation queue emits an UPDATE"
    )


def test_unchanged_status_still_produces_no_diff():
    """A genuinely unchanged status must not start emitting spurious patches."""
    existing_record = {"status": {"value": "active", "label": "Active"}}
    current = normalize_current_virtual_machine_payload(existing_record)

    current_status = ProxmoxToNetBoxVMStatus.from_proxmox(current["status"]).value
    desired_status = ProxmoxToNetBoxVMStatus.from_proxmox("running").value

    assert current_status == desired_status == "active"
