"""Tests for ProxmoxVmConfigInput parsing of kv-style flags (notably ``agent``)."""

from __future__ import annotations

import pytest

from proxbox_api.proxmox_to_netbox import parse_proxmox_tags
from proxbox_api.proxmox_to_netbox.models import (
    ProxmoxVmConfigInput,
    _parse_proxmox_kv_flag,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Bool / int / float passthroughs
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        (1.0, True),
        (0.0, False),
        # Plain string forms
        ("1", True),
        ("0", False),
        ("true", True),
        ("false", False),
        ("yes", True),
        ("no", False),
        ("on", True),
        ("off", False),
        # Whitespace / case
        ("  1  ", True),
        (" TRUE ", True),
        # Documented Proxmox kv strings
        ("1,fstrim_cloned_disks=1", True),
        ("0,fstrim_cloned_disks=1", False),
        ("1,fstrim_cloned_disks=1,type=virtio", True),
        ("enabled=1", True),
        ("enabled=0", False),
        ("enabled=1,fstrim_cloned_disks=1", True),
        ("enabled=0,fstrim_cloned_disks=1", False),
        ("enabled=1,freeze-fs-on-backup=0,type=isa", True),
        # Defensive cases
        (None, False),
        ("", False),
        ("   ", False),
        ("garbage", False),
        ("foo=bar", False),
        # Order-independent: enabled= can appear after another kv pair
        ("type=virtio,enabled=1", True),
        ("type=virtio,enabled=0", False),
    ],
)
def test_parse_proxmox_kv_flag(value: object, expected: bool) -> None:
    assert _parse_proxmox_kv_flag(value) is expected


@pytest.mark.parametrize(
    ("agent_value", "expected"),
    [
        (None, False),
        ("0", False),
        ("1", True),
        ("1,fstrim_cloned_disks=1", True),
        ("enabled=1,fstrim_cloned_disks=1", True),
        ("enabled=0,fstrim_cloned_disks=1", False),
    ],
)
def test_qemu_agent_enabled_through_model(agent_value: object, expected: bool) -> None:
    """Regression: end-to-end through Pydantic; covers the call site in
    sync_vm.py:1817 (`vm_config_obj.qemu_agent_enabled`).
    """
    config = ProxmoxVmConfigInput.model_validate({"agent": agent_value})
    assert config.qemu_agent_enabled is expected


def test_qemu_agent_enabled_default_when_absent() -> None:
    config = ProxmoxVmConfigInput.model_validate({})
    assert config.qemu_agent_enabled is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("   ", []),
        (";;;", []),
        ("critical", ["critical"]),
        ("critical;production", ["critical", "production"]),
        ("  critical ; production  ", ["critical", "production"]),
        ("Critical;PRODUCTION", ["critical", "production"]),
        # Duplicates collapse, preserving first-seen order
        ("critical;critical;production", ["critical", "production"]),
        ("critical;Production;production", ["critical", "production"]),
        # Empties between separators are skipped
        ("critical;;production", ["critical", "production"]),
        # Wrong type returns empty
        (123, []),
        ([], []),
    ],
)
def test_parse_proxmox_tags(raw: object, expected: list[str]) -> None:
    assert parse_proxmox_tags(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("critical;production", ["critical", "production"]),
        ("  Critical ; production ; critical ", ["critical", "production"]),
    ],
)
def test_proxmox_tags_through_model(raw: object, expected: list[str]) -> None:
    """End-to-end through Pydantic: VM config exposes parsed ``proxmox_tags``."""
    config = ProxmoxVmConfigInput.model_validate({"tags": raw})
    assert config.proxmox_tags == expected


def test_proxmox_tags_default_when_absent() -> None:
    config = ProxmoxVmConfigInput.model_validate({})
    assert config.proxmox_tags == []
