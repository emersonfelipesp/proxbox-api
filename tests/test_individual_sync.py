"""Tests for individual sync flows and status reporting."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from proxbox_api.dependencies import proxbox_tag
from proxbox_api.main import app
from proxbox_api.services.sync.individual.cluster_sync import sync_cluster_individual
from proxbox_api.services.sync.individual.helpers import (
    parse_disk_config_entry,
    resolve_proxmox_session,
)
from proxbox_api.services.sync.individual.snapshot_sync import sync_snapshot_individual
from proxbox_api.services.sync.individual.task_history_sync import sync_task_history_individual
from proxbox_api.services.sync.individual.vm_sync import (
    sync_vm_individual,
    sync_vm_with_related,
)
from proxbox_api.session.netbox import get_netbox_session
from proxbox_api.session.proxmox_providers import proxmox_sessions
from tests.factories.session import make_session, make_settings


class FakeRecord:
    def __init__(self, payload: dict[str, object], record_id: int = 1) -> None:
        self._payload = {"id": record_id, **payload}
        self.id = record_id

    def serialize(self) -> dict[str, object]:
        return dict(self._payload)


def test_resolve_proxmox_session_requires_exact_cluster_match():
    sessions = [SimpleNamespace(name="alpha"), SimpleNamespace(name="beta")]

    assert resolve_proxmox_session(sessions, "beta") is sessions[1]
    assert resolve_proxmox_session(sessions, "gamma") is None


def test_parse_disk_config_entry_preserves_leading_volume():
    parsed = parse_disk_config_entry("local-lvm:vm-101-disk-0,size=34359738368")

    assert parsed["volume"] == "local-lvm:vm-101-disk-0"
    assert parsed["size"] == "34359738368"


@pytest.mark.asyncio
async def test_individual_backup_route_supports_post(monkeypatch, test_api_key):
    captured: dict[str, object] = {}

    async def _fake_sync_backup(*args, **kwargs):
        captured.update(kwargs)
        return {"object_type": "backup", "cluster": "lab", "dry_run": kwargs["dry_run"]}

    app.dependency_overrides[get_netbox_session] = lambda: object()
    app.dependency_overrides[proxmox_sessions] = lambda: [SimpleNamespace(name="lab")]
    app.dependency_overrides[proxbox_tag] = lambda: SimpleNamespace(
        id=7, name="Proxbox", slug="proxbox", color="ff5722"
    )
    monkeypatch.setattr(
        "proxbox_api.routes.sync.individual.backup.sync_backup_individual",
        _fake_sync_backup,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Proxbox-API-Key": test_api_key},
    ) as client:
        response = await client.post(
            "/sync/individual/backup",
            params={
                "cluster_name": "lab",
                "node": "pve01",
                "storage": "local",
                "vmid": 101,
                "volid": "local:backup/vzdump-qemu-101.vma.zst",
                "auto_create_vm": "false",
                "auto_create_storage": "false",
                "dry_run": "true",
            },
        )

    assert response.status_code == 200
    assert response.json()["object_type"] == "backup"
    assert captured["auto_create_vm"] is False
    assert captured["auto_create_storage"] is False


@pytest.mark.asyncio
async def test_individual_ip_route_requires_cluster_name(test_api_key):
    app.dependency_overrides[get_netbox_session] = lambda: object()
    app.dependency_overrides[proxmox_sessions] = lambda: [SimpleNamespace(name="lab")]
    app.dependency_overrides[proxbox_tag] = lambda: SimpleNamespace(
        id=7, name="Proxbox", slug="proxbox", color="ff5722"
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Proxbox-API-Key": test_api_key},
    ) as client:
        response = await client.get(
            "/sync/individual/ip",
            params={
                "node": "pve01",
                "type": "qemu",
                "vmid": 101,
                "ip_address": "192.0.2.10/24",
            },
        )

    assert response.status_code == 422
    assert any(error["loc"][-1] == "cluster_name" for error in response.json()["detail"])


@pytest.mark.asyncio
async def test_sync_vm_individual_uses_real_proxmox_resource(monkeypatch):
    async def _fake_get_deps(self, cluster_name, node_name, vm_type):
        return (
            SimpleNamespace(id=10),
            SimpleNamespace(id=11),
            SimpleNamespace(id=12),
            SimpleNamespace(id=13),
            SimpleNamespace(id=14),
            SimpleNamespace(id=15),
            SimpleNamespace(id=16),
            SimpleNamespace(id=17),
            SimpleNamespace(id=18),
        )

    async def _fake_rest_list_async(*args, **kwargs):
        return []

    recorded_payload: dict[str, object] = {}

    async def _fake_rest_reconcile_async(*args, **kwargs):
        recorded_payload.update(kwargs["payload"])
        return FakeRecord(kwargs["payload"], record_id=55)

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.base.BaseIndividualSyncService._get_or_create_vm_dependencies",
        _fake_get_deps,
    )

    async def _fake_get_vm_config_individual(*args, **kwargs):
        return {"onboot": 1, "agent": 1}

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.vm_sync.get_vm_config_individual",
        _fake_get_vm_config_individual,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.vm_sync.get_vm_resource_individual",
        lambda *args, **kwargs: {
            "vmid": 101,
            "name": "db01",
            "node": "pve01",
            "type": "qemu",
            "status": "running",
            "maxcpu": 4,
            "maxmem": 8_000_000_000,
            "maxdisk": 120_000_000_000,
        },
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.vm_sync.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.vm_sync.rest_reconcile_async",
        _fake_rest_reconcile_async,
    )

    result = await sync_vm_individual(
        nb=object(),
        px=SimpleNamespace(name="lab"),
        tag=SimpleNamespace(id=7),
        cluster_name="lab",
        node="pve01",
        vm_type="qemu",
        vmid=101,
    )

    assert result["action"] == "created"
    assert result["proxmox_resource"]["name"] == "db01"
    assert result["proxmox_resource"]["status"] == "running"
    assert recorded_payload["name"] == "db01"
    assert recorded_payload["vcpus"] == 4


@pytest.mark.asyncio
async def test_sync_vm_with_related_gathers_interfaces_and_task_history(monkeypatch):
    interface_calls: list[str] = []
    task_history_calls: list[dict[str, object]] = []

    async def _fake_sync_vm_individual(*args, **kwargs):
        return {"object_type": "vm", "action": "created", "dependencies_synced": []}

    async def _fake_sync_interface_individual(
        nb, px, tag, node, vm_type, vmid, interface_name, auto_create_vm, dry_run
    ):
        interface_calls.append(interface_name)
        return {
            "object_type": "interface",
            "name": interface_name,
            "dependencies_synced": [{"object_type": "vm", "vmid": vmid}],
        }

    async def _fake_sync_task_history_individual(*args, **kwargs):
        task_history_calls.append(kwargs)
        return {"object_type": "task_history", "dependencies_synced": [{"object_type": "vm"}]}

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.vm_sync.sync_vm_individual",
        _fake_sync_vm_individual,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.vm_sync.sync_interface_individual",
        _fake_sync_interface_individual,
    )

    async def _fake_get_vm_config_individual(*args, **kwargs):
        return {
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
            "net1": "virtio=AA:BB:CC:DD:EE:00,bridge=vmbr1",
        }

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.vm_sync.get_vm_config_individual",
        _fake_get_vm_config_individual,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.task_history_sync.sync_task_history_individual",
        _fake_sync_task_history_individual,
    )

    result = await sync_vm_with_related(
        nb=object(),
        px=SimpleNamespace(name="lab"),
        tag=SimpleNamespace(id=7),
        cluster_name="lab",
        node="pve01",
        vm_type="qemu",
        vmid=101,
    )

    assert interface_calls == ["net0", "net1"]
    assert len(task_history_calls) == 1
    assert len(result["related"]) == 3


@pytest.mark.asyncio
async def test_sync_snapshot_individual_links_storage(monkeypatch):
    recorded_payload: dict[str, object] = {}

    async def _fake_rest_list_async(*args, **kwargs):
        path = args[1]
        query = kwargs.get("query", {})
        if path == "/api/virtualization/virtual-machines/":
            return [SimpleNamespace(id=44)]
        if path == "/api/plugins/proxbox/snapshots/" and query == {"vmid": 101, "name": "snap1"}:
            return []
        return []

    async def _fake_rest_reconcile_async(*args, **kwargs):
        recorded_payload.update(kwargs["payload"])
        return FakeRecord(kwargs["payload"], record_id=66)

    async def _fake_sync_storage(*args, **kwargs):
        return {"netbox_object": {"id": 33}}

    async def _fake_get_vm_snapshots(*args, **kwargs):
        return [{"name": "snap1", "description": "before-upgrade"}]

    async def _fake_get_vm_config(*args, **kwargs):
        return {"scsi0": "local-lvm:vm-101-disk-0,size=34359738368"}

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.snapshot_sync.get_vm_snapshots_individual",
        _fake_get_vm_snapshots,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.snapshot_sync.get_vm_config_individual",
        _fake_get_vm_config,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.snapshot_sync.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.snapshot_sync.rest_reconcile_async",
        _fake_rest_reconcile_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.storage_sync.sync_storage_individual",
        _fake_sync_storage,
    )

    result = await sync_snapshot_individual(
        nb=object(),
        px=SimpleNamespace(name="lab"),
        tag=SimpleNamespace(id=7),
        cluster_name="lab",
        node="pve01",
        vm_type="qemu",
        vmid=101,
        snapshot_name="snap1",
    )

    assert result["action"] == "created"
    assert recorded_payload["proxmox_storage"] == 33
    assert {"object_type": "storage", "name": "local-lvm", "action": "created"} in result[
        "dependencies_synced"
    ]


@pytest.mark.asyncio
async def test_sync_cluster_individual_reports_real_drift_status(monkeypatch):
    """Post-#357: the reported action is the real ``upsert_*`` outcome.

    Previously the action was a heuristic based on whether the GET found an
    existing record, which mislabeled no-op syncs as ``updated``. The
    migration to ``upsert_cluster`` makes the action mirror the underlying
    ``ReconcileResult.status``.
    """
    from proxbox_api.services import netbox_writers
    from proxbox_api.services.netbox_writers import UpsertResult

    async def _fake_rest_list_async(_nb, _path, query=None):
        return []

    async def _fake_upsert_cluster_type(_nb, *, mode, tag_refs):
        return UpsertResult(record=SimpleNamespace(id=7), status="unchanged")

    async def _fake_upsert_cluster(_nb, **kwargs):
        return UpsertResult(
            record=SimpleNamespace(id=1, serialize=lambda: {"id": 1}),
            status="updated",
        )

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.upsert_cluster_type",
        _fake_upsert_cluster_type,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.upsert_cluster",
        _fake_upsert_cluster,
    )

    ctx = make_session(
        nb=object(),
        px_sessions=[SimpleNamespace(name="lab")],
        tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
        settings=make_settings(),
        operation_id="test-cluster-drift-status",
    )
    result = await sync_cluster_individual(ctx, "lab")

    assert result["action"] == "updated"
    assert {"object_type": "cluster_type", "action": "unchanged"} in result["dependencies_synced"]
    # Silence linter: helper referenced for the import side-effect.
    assert netbox_writers.UpsertResult is UpsertResult


@pytest.mark.asyncio
async def test_sync_backup_individual_reports_updated_when_backup_exists(monkeypatch):
    from proxbox_api.services.sync.individual.backup_sync import sync_backup_individual

    async def _fake_rest_list_async(_nb, path, query=None):
        if path == "/api/virtualization/virtual-machines/":
            return [SimpleNamespace(id=44)]
        if path == "/api/plugins/proxbox/backups/" and query == {
            "vmid": "101",
            "volume_id": "local:backup/vm-101",
        }:
            return [SimpleNamespace(id=55)]
        if path == "/api/plugins/proxbox/storage/":
            return [SimpleNamespace(id=33)]
        return []

    async def _fake_rest_reconcile_async(*args, **kwargs):
        return FakeRecord(kwargs["payload"], record_id=55)

    async def _fake_get_vm_backups_individual(*args, **kwargs):
        return [{"volid": "local:backup/vm-101", "size": 10, "format": "vma"}]

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.backup_sync.get_vm_backups_individual",
        _fake_get_vm_backups_individual,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.backup_sync.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.helpers.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.backup_sync.rest_reconcile_async",
        _fake_rest_reconcile_async,
    )
    # Also patch in helpers module where ensure_vm_record is defined
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.helpers.rest_list_async",
        _fake_rest_list_async,
    )

    result = await sync_backup_individual(
        nb=object(),
        px=SimpleNamespace(name="lab"),
        tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
        node="pve01",
        storage="local",
        vmid=101,
        volid="local:backup/vm-101",
        auto_create_storage=False,
    )

    assert result["action"] == "updated"


@pytest.mark.asyncio
async def test_individual_interface_route_supports_post(monkeypatch, test_api_key):
    async def _fake_sync_interface(*args, **kwargs):
        return {"object_type": "interface", "name": "net0", "dry_run": kwargs["dry_run"]}

    app.dependency_overrides[get_netbox_session] = lambda: object()
    app.dependency_overrides[proxmox_sessions] = lambda: [SimpleNamespace(name="lab")]
    app.dependency_overrides[proxbox_tag] = lambda: SimpleNamespace(
        id=7, name="Proxbox", slug="proxbox", color="ff5722"
    )
    monkeypatch.setattr(
        "proxbox_api.routes.sync.individual.interface.sync_interface_individual",
        _fake_sync_interface,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Proxbox-API-Key": test_api_key},
    ) as client:
        response = await client.post(
            "/sync/individual/interface",
            params={
                "cluster_name": "lab",
                "node": "pve01",
                "type": "qemu",
                "vmid": 101,
                "interface_name": "net0",
                "dry_run": "true",
            },
        )

    assert response.status_code == 200
    assert response.json()["object_type"] == "interface"


@pytest.mark.asyncio
async def test_individual_disk_route_forwards_auto_create_flags(monkeypatch, test_api_key):
    captured: dict[str, object] = {}

    async def _fake_sync_disk(*args, **kwargs):
        captured.update(kwargs)
        return {"object_type": "disk", "dry_run": kwargs["dry_run"]}

    app.dependency_overrides[get_netbox_session] = lambda: object()
    app.dependency_overrides[proxmox_sessions] = lambda: [SimpleNamespace(name="lab")]
    app.dependency_overrides[proxbox_tag] = lambda: SimpleNamespace(
        id=7, name="Proxbox", slug="proxbox", color="ff5722"
    )
    monkeypatch.setattr(
        "proxbox_api.routes.sync.individual.disk.sync_virtual_disk_individual",
        _fake_sync_disk,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Proxbox-API-Key": test_api_key},
    ) as client:
        response = await client.post(
            "/sync/individual/disk",
            params={
                "cluster_name": "lab",
                "node": "pve01",
                "type": "qemu",
                "vmid": 101,
                "disk_name": "scsi0",
                "auto_create_vm": "false",
                "auto_create_storage": "false",
                "dry_run": "true",
            },
        )

    assert response.status_code == 200
    assert captured["auto_create_vm"] is False
    assert captured["auto_create_storage"] is False


@pytest.mark.asyncio
async def test_individual_snapshot_route_forwards_auto_create_flags(monkeypatch, test_api_key):
    captured: dict[str, object] = {}

    async def _fake_sync_snapshot(*args, **kwargs):
        captured.update(kwargs)
        return {"object_type": "snapshot", "dry_run": kwargs["dry_run"]}

    app.dependency_overrides[get_netbox_session] = lambda: object()
    app.dependency_overrides[proxmox_sessions] = lambda: [SimpleNamespace(name="lab")]
    app.dependency_overrides[proxbox_tag] = lambda: SimpleNamespace(
        id=7, name="Proxbox", slug="proxbox", color="ff5722"
    )
    monkeypatch.setattr(
        "proxbox_api.routes.sync.individual.snapshot.sync_snapshot_individual",
        _fake_sync_snapshot,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Proxbox-API-Key": test_api_key},
    ) as client:
        response = await client.post(
            "/sync/individual/snapshot",
            params={
                "cluster_name": "lab",
                "node": "pve01",
                "type": "qemu",
                "vmid": 101,
                "snapshot_name": "pre-upgrade",
                "auto_create_vm": "false",
                "auto_create_storage": "false",
                "dry_run": "true",
            },
        )

    assert response.status_code == 200
    assert captured["auto_create_vm"] is False
    assert captured["auto_create_storage"] is False


@pytest.mark.asyncio
async def test_individual_task_history_route_uses_explicit_cluster(monkeypatch, test_api_key):
    captured: dict[str, object] = {}

    async def _fake_sync_task_history_individual(*args, **kwargs):
        captured.update(kwargs)
        return {"object_type": "task_history", "cluster_name": kwargs["cluster_name"]}

    app.dependency_overrides[get_netbox_session] = lambda: object()
    app.dependency_overrides[proxmox_sessions] = lambda: [
        SimpleNamespace(name="alpha"),
        SimpleNamespace(name="beta"),
    ]
    app.dependency_overrides[proxbox_tag] = lambda: SimpleNamespace(
        id=7, name="Proxbox", slug="proxbox", color="ff5722"
    )
    monkeypatch.setattr(
        "proxbox_api.routes.sync.individual.task_history.sync_task_history_individual",
        _fake_sync_task_history_individual,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Proxbox-API-Key": test_api_key},
    ) as client:
        response = await client.get(
            "/sync/individual/task-history",
            params={
                "cluster_name": "beta",
                "node": "pve01",
                "type": "qemu",
                "vmid": 101,
                "upid": "UPID:1",
            },
        )

    assert response.status_code == 200
    assert captured["cluster_name"] == "beta"
    assert response.json()["cluster_name"] == "beta"


@pytest.mark.asyncio
async def test_individual_task_history_route_requires_cluster_for_multi_session(test_api_key):
    app.dependency_overrides[get_netbox_session] = lambda: object()
    app.dependency_overrides[proxmox_sessions] = lambda: [
        SimpleNamespace(name="alpha"),
        SimpleNamespace(name="beta"),
    ]
    app.dependency_overrides[proxbox_tag] = lambda: SimpleNamespace(
        id=7, name="Proxbox", slug="proxbox", color="ff5722"
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Proxbox-API-Key": test_api_key},
    ) as client:
        response = await client.get(
            "/sync/individual/task-history",
            params={
                "node": "pve01",
                "type": "qemu",
                "vmid": 101,
                "upid": "UPID:1",
            },
        )

    assert response.status_code == 400
    assert "Multiple Proxmox sessions configured" in response.json()["message"]


@pytest.mark.asyncio
async def test_sync_task_history_individual_accepts_cluster_name_and_reports_updated(monkeypatch):
    async def _fake_rest_list_async(_nb, path, query=None):
        if path == "/api/virtualization/virtual-machines/":
            return [SimpleNamespace(id=44)]
        if path == "/api/plugins/proxbox/task-history/" and (query or {}).get("upid") == "UPID:1":
            return [SimpleNamespace(id=77)]
        return []

    async def _fake_rest_reconcile_async(*args, **kwargs):
        return FakeRecord(kwargs["payload"], record_id=77)

    async def _fake_get_vm_tasks_individual(*args, **kwargs):
        return [{"upid": "UPID:1", "type": "qmstart", "user": "root"}]

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.task_history_sync.get_vm_tasks_individual",
        _fake_get_vm_tasks_individual,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.task_history_sync.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.task_history_sync.rest_reconcile_async",
        _fake_rest_reconcile_async,
    )

    result = await sync_task_history_individual(
        nb=object(),
        px=SimpleNamespace(name="lab"),
        tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
        node="pve01",
        vm_type="qemu",
        vmid=101,
        cluster_name="lab",
    )

    assert result["action"] == "updated"
