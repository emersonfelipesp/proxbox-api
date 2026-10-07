from __future__ import annotations

import logging

import pytest

from proxbox_api.services.sync import virtual_disks


def _disk(record_id: int, name: str, *, tagged: bool, size: int = 10) -> dict[str, object]:
    return {
        "id": record_id,
        "name": name,
        "size": size,
        "tags": [{"name": "Proxbox", "slug": "proxbox"}] if tagged else [],
    }


class _Harness(list):
    def __init__(self, ids, rows):
        super().__init__()
        self._ids = ids
        self.rows = rows

    def __eq__(self, other):
        return self._ids == other

    def __iter__(self):
        return iter(self._ids)


@pytest.fixture
def deleted(monkeypatch) -> _Harness:
    ids: list[int] = []
    rows: list[dict[str, object]] = []

    async def _list(_nb, _path, *, query=None):
        return list(rows)

    async def _delete(_nb, _path, record_ids):
        ids.extend(record_ids)
        return len(record_ids)

    monkeypatch.setattr(virtual_disks, "rest_list_async", _list)
    monkeypatch.setattr(virtual_disks, "rest_bulk_delete_async", _delete)
    return _Harness(ids, rows)


@pytest.mark.asyncio
async def test_stale_tagged_disk_is_deleted(deleted):
    deleted.rows.extend([_disk(1, "scsi0", tagged=True), _disk(2, "scsi1", tagged=True)])
    count = await virtual_disks._delete_stale_virtual_disks(
        object(), vm_id=5, desired_disks={"scsi0": 10}
    )
    assert count == 1
    assert deleted == [2]


@pytest.mark.asyncio
async def test_untagged_operator_disk_is_never_deleted(deleted):
    deleted.rows.extend(
        [
            _disk(1, "scsi0", tagged=True),
            _disk(2, "operator-disk", tagged=False),
            _disk(3, "scsi1", tagged=True),
        ]
    )
    count = await virtual_disks._delete_stale_virtual_disks(
        object(), vm_id=5, desired_disks={"scsi0": 10}
    )
    assert deleted == [3]
    assert count == 1


@pytest.mark.asyncio
async def test_empty_desired_set_deletes_nothing(deleted, caplog):
    deleted.rows.extend([_disk(1, "scsi0", tagged=True), _disk(2, "scsi1", tagged=True)])
    proxbox_logger = logging.getLogger("proxbox")
    proxbox_logger.addHandler(caplog.handler)
    try:
        count = await virtual_disks._delete_stale_virtual_disks(object(), vm_id=5, desired_disks={})
    finally:
        proxbox_logger.removeHandler(caplog.handler)
    assert count == 0
    assert deleted == []
    assert "no desired disks parsed" in caplog.text


@pytest.mark.asyncio
async def test_unparseable_but_present_disk_keeps_its_record(deleted):
    """scsi1 is attached (passthrough, no size) so its record stays; scsi2 is gone."""
    deleted.rows.extend(
        [
            _disk(1, "scsi0", tagged=True),
            _disk(2, "scsi1", tagged=True),
            _disk(3, "scsi2", tagged=True),
        ]
    )
    count = await virtual_disks._delete_stale_virtual_disks(
        object(),
        vm_id=5,
        desired_disks={"scsi0": 10},
        present_disk_names=frozenset({"scsi0", "scsi1"}),
    )
    assert deleted == [3]
    assert count == 1


def test_present_disk_names_includes_disks_the_parser_skips():
    from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput

    config = ProxmoxVmConfigInput.model_validate(
        {
            "scsi0": "local:vm-100-disk-0,size=32G",
            "scsi1": "/dev/sdb",
            "ide2": "none,media=cdrom",
            "unused0": "local:vm-100-disk-9",
            "name": "vm100",
        }
    )
    assert {disk.name for disk in config.disks} == {"scsi0", "ide2"}
    assert virtual_disks._present_disk_names(config) == frozenset({"scsi0", "scsi1", "ide2"})
