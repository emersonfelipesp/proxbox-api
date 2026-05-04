"""Regression tests for Proxmox disk parsing (issue netbox-proxbox#349).

These tests pin three behaviors that, if reverted, re-introduce the
``virtualmachine.disk`` ↔ ``virtual-disks`` aggregate-mismatch failure on
NetBox 4.5+:

1. Passthrough disks (``scsiN: /dev/sdb``) are skipped with a WARN — they
   carry no ``size=`` and cannot be represented as virtual-disks.
2. ``efidisk0`` and ``tpmstate0`` ARE counted (firmware/TPM state disks have a
   real ``size=`` and must appear on both sides of the aggregate).
3. The VM-level ``disk`` is always the sum of parsed virtual-disk children;
   it never falls back to the cluster ``maxdisk``.
"""

from __future__ import annotations

import logging

import pytest

from proxbox_api.proxmox_to_netbox.schemas.disks import (
    parse_disk_entry,
    parse_vm_config_disks,
    size_str_to_mb,
)


@pytest.fixture
def proxbox_caplog(caplog):
    """Attach caplog to the non-propagating ``proxbox`` logger."""
    proxbox_logger = logging.getLogger("proxbox")
    proxbox_logger.addHandler(caplog.handler)
    proxbox_logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        proxbox_logger.removeHandler(caplog.handler)


def test_parse_disk_entry_regular_scsi_with_size():
    entry = parse_disk_entry("scsi0", "local-lvm:vm-100-disk-0,size=32G,format=qcow2")
    assert entry is not None
    assert entry.name == "scsi0"
    assert entry.size == 32 * 1024  # 32 GiB → 32768 MiB
    assert entry.format == "qcow2"
    assert entry.storage_name == "local-lvm"


def test_parse_disk_entry_virtio_counted():
    entry = parse_disk_entry("virtio0", "local-lvm:vm-100-disk-0,size=40G")
    assert entry is not None
    assert entry.size == 40 * 1024


def test_parse_disk_entry_rootfs_lxc_counted():
    entry = parse_disk_entry("rootfs", "local-lvm:vm-100-disk-0,size=8G")
    assert entry is not None
    assert entry.name == "rootfs"
    assert entry.size == 8 * 1024


def test_parse_disk_entry_efidisk_counted():
    entry = parse_disk_entry("efidisk0", "local-lvm:vm-100-disk-1,size=4M")
    assert entry is not None
    assert entry.name == "efidisk0"
    # 4M parses to 4 MiB by ``size_str_to_mb``.
    assert entry.size == 4


def test_parse_disk_entry_tpmstate_counted():
    entry = parse_disk_entry("tpmstate0", "local-lvm:vm-100-disk-2,size=4M")
    assert entry is not None
    assert entry.name == "tpmstate0"
    assert entry.size == 4


def test_parse_disk_entry_passthrough_returns_none_and_warns(proxbox_caplog):
    """``scsi2: /dev/sdb`` has no ``size=`` field. Parser must drop it and
    emit a structured WARN so operators can correlate which disks were
    excluded from the NetBox aggregate.
    """
    proxbox_caplog.set_level(logging.WARNING)
    assert parse_disk_entry("scsi2", "/dev/sdb") is None
    assert any(
        "scsi2" in record.message and "size" in record.message.lower()
        for record in proxbox_caplog.records
        if record.levelno == logging.WARNING
    )


def test_parse_disk_entry_unused_returns_none_silently(proxbox_caplog):
    """``unusedN`` is the documented Proxmox parking slot for detached disks.
    Skip silently — no WARN, since no NetBox object is expected for it.
    """
    proxbox_caplog.set_level(logging.WARNING)
    assert parse_disk_entry("unused0", "local-lvm:vm-100-disk-3,size=10G") is None
    assert not any(record.levelno == logging.WARNING for record in proxbox_caplog.records)


def test_parse_disk_entry_unknown_key_returns_none():
    # Keys outside the recognized disk families (e.g. ``net0``, ``boot``) are
    # not disks — drop without a WARN.
    assert parse_disk_entry("net0", "virtio=DE:AD:BE:EF:00:01,bridge=vmbr0") is None


def test_parse_disk_entry_non_string_value_returns_none():
    assert parse_disk_entry("scsi0", 123) is None  # type: ignore[arg-type]
    assert parse_disk_entry("scsi0", None) is None  # type: ignore[arg-type]


def test_parse_vm_config_disks_aggregate_excludes_passthrough(proxbox_caplog):
    """The aggregate the mapper sends to NetBox must equal the sum of just the
    parseable disks — the passthrough silently failing the parser is what
    used to break ``disk == sum(virtual-disks)`` on update.
    """
    proxbox_caplog.set_level(logging.WARNING)
    config = {
        "scsi0": "local-lvm:vm-100-disk-0,size=32G",
        "scsi1": "local-lvm:vm-100-disk-1,size=64G",
        "scsi2": "/dev/sdb",  # passthrough — dropped
        "efidisk0": "local-lvm:vm-100-disk-2,size=4M",
        "name": "ignored-non-disk-key",
    }
    entries = parse_vm_config_disks(config)
    names = {e.name for e in entries}
    assert names == {"scsi0", "scsi1", "efidisk0"}

    # Aggregate equals what a downstream mapper will send as VM.disk and is
    # also the sum of POSTed virtual-disks — the two MUST match.
    aggregate = sum(e.size for e in entries)
    assert aggregate == 32 * 1024 + 64 * 1024 + 4

    # Operator gets a WARN for the dropped passthrough.
    assert any("scsi2" in r.message for r in proxbox_caplog.records)


def test_parse_vm_config_disks_empty_when_no_disks():
    """A config with no disk-shaped keys aggregates to an empty list. The
    mapper's downstream ``disk_mb`` then reports 0 — no maxdisk fallback.
    """
    assert parse_vm_config_disks({"onboot": 1, "agent": 1, "name": "boring"}) == []


def test_size_str_to_mb_units():
    assert size_str_to_mb("0") == 0
    assert size_str_to_mb("") == 0
    assert size_str_to_mb("1M") == 1
    assert size_str_to_mb("1G") == 1024
    assert size_str_to_mb("1T") == 1024 * 1024
    assert size_str_to_mb("32G") == 32 * 1024
    assert size_str_to_mb("garbage") == 0
