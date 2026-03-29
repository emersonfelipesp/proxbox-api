from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime
from types import SimpleNamespace

from netbox_sdk.client import ApiResponse
import pytest
from fastapi import HTTPException

from proxbox_api.database import NetBoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.main import create_sync_process, full_update_sync, get_sync_processes, standalone_info
from proxbox_api.netbox_sdk_sync import SyncProxy
from proxbox_api.services.sync.devices import create_proxmox_devices
from proxbox_api.routes.netbox import (
    create_netbox_endpoint,
    delete_netbox_endpoint,
    get_netbox_endpoint,
    get_netbox_endpoints,
    netbox_openapi,
    netbox_status,
    update_netbox_endpoint,
)
from proxbox_api.routes.virtualization.virtual_machines import (
    create_netbox_backups,
    create_virtual_machines,
)
from proxbox_api.routes.proxmox.endpoints import (
    ProxmoxEndpointCreate,
    ProxmoxEndpointUpdate,
    create_proxmox_endpoint,
    delete_proxmox_endpoint,
    get_proxmox_endpoint,
    get_proxmox_endpoints,
    update_proxmox_endpoint,
)


def test_root_route_returns_service_metadata():
    body = asyncio.run(standalone_info())
    assert body["message"] == "Proxbox Backend made in FastAPI framework"
    assert body["proxbox"]["github"].endswith("netbox-proxbox")


def test_sync_entrypoints_share_proxbox_tag_dependency():
    for entrypoint in (create_proxmox_devices, create_virtual_machines, full_update_sync):
        assert "tag" in inspect.signature(entrypoint).parameters


def test_create_proxmox_devices_uses_request_scoped_rest_session():
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            self.calls.append((method, path, query, payload, expect_json))

            if method == "POST" and path == "/api/plugins/proxbox/sync-processes/":
                return ApiResponse(
                    status=201,
                    text=json.dumps(
                        {
                            "id": 1,
                            "name": "sync-devices",
                            "status": "not-started",
                            "url": "https://netbox.local/api/plugins/proxbox/sync-processes/1/",
                        }
                    ),
                )
            if method == "PATCH" and path == "/api/plugins/proxbox/sync-processes/1/":
                body = {
                    "id": 1,
                    "name": "sync-devices",
                    "status": payload.get("status", "completed"),
                    "url": "https://netbox.local/api/plugins/proxbox/sync-processes/1/",
                }
                return ApiResponse(status=200, text=json.dumps(body))

            lookup_key = (method, path, tuple(sorted((query or {}).items())))
            if lookup_key in lookup_responses:
                return ApiResponse(status=200, text=json.dumps(lookup_responses[lookup_key]))
            if (method, path) in create_responses:
                return ApiResponse(status=201, text=json.dumps(create_responses[(method, path)]))
            raise AssertionError((method, path, query, payload, expect_json))

    lookup_responses = {
        ("GET", "/api/virtualization/cluster-types/", (("limit", 2), ("slug", "cluster"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/virtualization/cluster-types/", (("limit", 2), ("name", "Cluster"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/virtualization/clusters/", (("limit", 2), ("name", "lab"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/dcim/manufacturers/", (("limit", 2), ("slug", "proxmox"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/dcim/manufacturers/", (("limit", 2), ("name", "Proxmox"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/dcim/device-types/", (("limit", 2), ("model", "Proxmox Generic Device"))): {
            "count": 0,
            "results": [],
        },
        (
            "GET",
            "/api/dcim/device-types/",
            (("limit", 2), ("manufacturer_id", 13), ("model", "Proxmox Generic Device")),
        ): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/dcim/device-types/", (("limit", 2), ("slug", "proxmox-generic-device"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/dcim/device-roles/", (("limit", 2), ("slug", "proxmox-node"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/dcim/device-roles/", (("limit", 2), ("name", "Proxmox Node"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/dcim/sites/", (("limit", 2), ("slug", "proxmox-default-site-lab"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/dcim/sites/", (("limit", 2), ("name", "Proxmox Default Site - lab"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/dcim/devices/", (("limit", 2), ("name", "pve01"))): {
            "count": 0,
            "results": [],
        },
        ("GET", "/api/dcim/devices/", (("limit", 2), ("name", "pve01"), ("site_id", 16))): {
            "count": 0,
            "results": [],
        },
    }
    create_responses = {
        ("POST", "/api/virtualization/cluster-types/"): {"id": 11, "name": "Cluster"},
        ("POST", "/api/virtualization/clusters/"): {"id": 12, "name": "lab"},
        ("POST", "/api/dcim/manufacturers/"): {"id": 13, "name": "Proxmox"},
        ("POST", "/api/dcim/device-types/"): {"id": 14, "model": "Proxmox Generic Device"},
        ("POST", "/api/dcim/device-roles/"): {"id": 15, "name": "Proxmox Node"},
        ("POST", "/api/dcim/sites/"): {"id": 16, "name": "Proxmox Default Site - lab"},
        ("POST", "/api/dcim/devices/"): {"id": 17, "name": "pve01"},
    }

    fake_session = SimpleNamespace(
        client=FakeClient(),
        extras=SimpleNamespace(journal_entries=SimpleNamespace(create=lambda payload: payload)),
    )
    cluster_status = [
        SimpleNamespace(
            name="lab",
            mode="cluster",
            node_list=[SimpleNamespace(name="pve01")],
        )
    ]
    tag = SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722")

    result = asyncio.run(
        create_proxmox_devices(
            netbox_session=fake_session,
            clusters_status=cluster_status,
            tag=tag,
        )
    )

    assert result == [{"id": 17, "name": "pve01"}]
    device_create = next(
        payload
        for method, path, _query, payload, _expect_json in fake_session.client.calls
        if method == "POST" and path == "/api/dcim/devices/"
    )
    assert device_create["tags"] == [{"name": "Proxbox", "slug": "proxbox", "color": "ff5722"}]


def test_create_proxmox_devices_surfaces_real_netbox_detail():
    class FakeClient:
        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            if method == "POST" and path == "/api/plugins/proxbox/sync-processes/":
                return ApiResponse(
                    status=201,
                    text=json.dumps(
                        {
                            "id": 1,
                            "name": "sync-devices",
                            "status": "not-started",
                            "url": "https://netbox.local/api/plugins/proxbox/sync-processes/1/",
                        }
                    ),
                )
            if method == "PATCH" and path == "/api/plugins/proxbox/sync-processes/1/":
                body = {
                    "id": 1,
                    "name": "sync-devices",
                    "status": payload.get("status", "failed"),
                    "url": "https://netbox.local/api/plugins/proxbox/sync-processes/1/",
                }
                return ApiResponse(status=200, text=json.dumps(body))
            if method == "GET":
                return ApiResponse(status=200, text=json.dumps({"count": 0, "results": []}))
            if method == "POST" and path == "/api/dcim/devices/":
                return ApiResponse(
                    status=400,
                    text=json.dumps({"detail": "tags: expected object, got integer"}),
                )
            return ApiResponse(status=201, text=json.dumps({"id": 99, "name": "placeholder"}))

    fake_session = SimpleNamespace(
        client=FakeClient(),
        extras=SimpleNamespace(journal_entries=SimpleNamespace(create=lambda payload: payload)),
    )
    cluster_status = [
        SimpleNamespace(
            name="lab",
            mode="cluster",
            node_list=[SimpleNamespace(name="pve01")],
        )
    ]
    tag = SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722")

    with pytest.raises(
        ProxboxException,
        match="Error during device sync: Error creating NetBox device",
    ) as excinfo:
        asyncio.run(
            create_proxmox_devices(
                netbox_session=fake_session,
                clusters_status=cluster_status,
                tag=tag,
            )
        )
    assert excinfo.value.detail == "tags: expected object, got integer"

def test_sync_process_routes_use_rest_helpers(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            self.calls.append((method, path, query, payload, expect_json))
            if method == "GET":
                body = {
                    "count": 1,
                    "results": [
                        {
                            "id": 5,
                            "name": "sync-all",
                            "sync_type": "all",
                            "status": "completed",
                            "started_at": "2025-03-13T15:08:09.051478Z",
                            "completed_at": "2025-03-13T15:08:09.051478Z",
                            "url": "https://netbox.local/api/plugins/proxbox/sync-processes/5/",
                            "display": "sync-all (all)",
                        }
                    ],
                }
            else:
                body = {
                    "id": 6,
                    "name": "sync-all",
                    "sync_type": "all",
                    "status": "not-started",
                    "started_at": "2025-03-13T15:08:09.051478Z",
                    "completed_at": "2025-03-13T15:08:09.051478Z",
                    "url": "https://netbox.local/api/plugins/proxbox/sync-processes/6/",
                    "display": "sync-all (all)",
                }
            return ApiResponse(status=200 if method == "GET" else 201, text=json.dumps(body))

    fake_session = SyncProxy(type("FakeApi", (), {"client": FakeClient()})())
    monkeypatch.setattr("proxbox_api.main.get_raw_netbox_session", lambda: fake_session)

    listed = asyncio.run(get_sync_processes())
    created = asyncio.run(create_sync_process())

    assert listed[0]["sync_type"] == "all"
    assert created.sync_type == "all"
    assert fake_session.client.calls[0][0:2] == ("GET", "/api/plugins/proxbox/sync-processes/")
    assert fake_session.client.calls[1][0:2] == ("POST", "/api/plugins/proxbox/sync-processes/")


def test_proxmox_endpoint_crud_lifecycle(db_session):
    created = create_proxmox_endpoint(
        ProxmoxEndpointCreate(
            name="pve-lab-1",
            ip_address="10.0.0.10",
            domain="pve-lab-1.local",
            port=8006,
            username="root@pam",
            password="supersecret",
            verify_ssl=False,
        ),
        db_session,
    )
    endpoint_id = created.id
    assert created.name == "pve-lab-1"
    assert created.password == "supersecret"

    listed = get_proxmox_endpoints(db_session)
    assert len(listed) == 1

    updated = update_proxmox_endpoint(
        endpoint_id,
        ProxmoxEndpointUpdate(
            name="pve-lab-1-updated",
            verify_ssl=True,
            token_name="sync",
            token_value="secret-token",
            password=None,
        ),
        db_session,
    )
    assert updated.name == "pve-lab-1-updated"
    assert updated.token_name == "sync"
    assert updated.verify_ssl is True

    deleted = delete_proxmox_endpoint(endpoint_id, db_session)
    assert deleted == {"message": "Proxmox endpoint deleted."}

    with pytest.raises(HTTPException, match="Proxmox endpoint not found"):
        get_proxmox_endpoint(endpoint_id, db_session)


def test_proxmox_endpoint_requires_complete_token_pair(db_session):
    with pytest.raises(
        HTTPException,
        match="token_name and token_value must be provided together",
    ):
        create_proxmox_endpoint(
            ProxmoxEndpointCreate(
                name="pve-lab-2",
                ip_address="10.0.0.11",
                port=8006,
                username="root@pam",
                token_name="sync",
                verify_ssl=True,
            ),
            db_session,
        )


def test_netbox_endpoint_crud_and_singleton_rule(db_session):
    payload = NetBoxEndpoint(
        name="netbox-primary",
        ip_address="10.0.0.20",
        domain="netbox.local",
        port=443,
        token="token-1",
        verify_ssl=True,
    )
    created = create_netbox_endpoint(payload, db_session)
    endpoint_id = created.id

    with pytest.raises(HTTPException, match="Only one NetBox endpoint is allowed"):
        create_netbox_endpoint(
            NetBoxEndpoint(
                name="netbox-secondary",
                ip_address="10.0.0.21",
                domain="netbox2.local",
                port=443,
                token="token-2",
                verify_ssl=True,
            ),
            db_session,
        )

    listed = get_netbox_endpoints(db_session)
    assert len(listed) == 1

    updated = update_netbox_endpoint(
        endpoint_id,
        NetBoxEndpoint(
            name="netbox-primary-updated",
            ip_address="10.0.0.20",
            domain="netbox.local",
            port=443,
            token="token-2",
            verify_ssl=True,
        ),
        db_session,
    )
    assert updated.name == "netbox-primary-updated"

    assert get_netbox_endpoint(endpoint_id, db_session).token == "token-2"
    assert delete_netbox_endpoint(endpoint_id, db_session) == {
        "message": "NetBox Endpoint deleted."
    }


def test_netbox_endpoint_rejects_v1_without_token(db_session):
    with pytest.raises(HTTPException, match="token is required for NetBox API token v1"):
        create_netbox_endpoint(
            NetBoxEndpoint(
                name="netbox-primary",
                ip_address="10.0.0.20",
                domain="netbox.local",
                port=443,
                token_version="v1",
                token="",
                verify_ssl=True,
            ),
            db_session,
        )


def test_netbox_endpoint_rejects_v2_incomplete_token(db_session):
    with pytest.raises(
        HTTPException,
        match="token_key and token \\(secret\\) must both be set",
    ):
        create_netbox_endpoint(
            NetBoxEndpoint(
                name="netbox-primary",
                ip_address="10.0.0.20",
                domain="netbox.local",
                port=443,
                token_version="v2",
                token_key="myid",
                token="",
                verify_ssl=True,
            ),
            db_session,
        )


def test_netbox_endpoint_accepts_v2_token(db_session):
    created = create_netbox_endpoint(
        NetBoxEndpoint(
            name="netbox-v2",
            ip_address="10.0.0.20",
            domain="netbox.local",
            port=443,
            token_version="v2",
            token_key="myid",
            token="secretpart",
            verify_ssl=True,
        ),
        db_session,
    )
    assert created.token_version == "v2"
    assert created.token_key == "myid"
    assert created.token == "secretpart"


def test_netbox_status_and_openapi_routes_are_mocked(client_with_fake_netbox):
    fake_session = client_with_fake_netbox

    status_body = asyncio.run(netbox_status(fake_session))
    assert status_body["status"] == "ok"

    openapi_body = asyncio.run(netbox_openapi(fake_session))
    assert "/api/virtualization/virtual-machines/" in openapi_body["paths"]


def test_netbox_status_route_wraps_dependency_errors():
    class BrokenNetBoxSession:
        def status(self):
            raise RuntimeError("boom")

    with pytest.raises(ProxboxException, match="Error fetching status from NetBox API."):
        asyncio.run(netbox_status(BrokenNetBoxSession()))


def test_full_update_sync_returns_structured_payload(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.main.create_proxmox_devices",
        lambda **kwargs: asyncio.sleep(0, result=[{"id": 1, "name": "node01"}]),
    )
    monkeypatch.setattr(
        "proxbox_api.main.create_virtual_machines",
        lambda **kwargs: asyncio.sleep(0, result=[{"id": 101, "name": "vm01"}]),
    )

    body = asyncio.run(
        full_update_sync(
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=type("Tag", (), {"id": 1})(),
        )
    )

    assert body == {
        "status": "completed",
        "devices": [{"id": 1, "name": "node01"}],
        "virtual_machines": [{"id": 101, "name": "vm01"}],
        "devices_count": 1,
        "virtual_machines_count": 1,
    }


def test_full_update_sync_handles_empty_device_result(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.main.create_proxmox_devices",
        lambda **kwargs: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        "proxbox_api.main.create_virtual_machines",
        lambda **kwargs: asyncio.sleep(0, result=[]),
    )

    body = asyncio.run(
        full_update_sync(
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=type("Tag", (), {"id": 1})(),
        )
    )

    assert body["devices"] == []
    assert body["virtual_machines"] == []
    assert body["devices_count"] == 0
    assert body["virtual_machines_count"] == 0


def test_full_update_sync_reraises_device_phase_proxbox_exception(monkeypatch):
    async def _fail_devices(**kwargs):
        raise ProxboxException(message="Error while syncing nodes.", detail="device failed")

    monkeypatch.setattr("proxbox_api.main.create_proxmox_devices", _fail_devices)

    with pytest.raises(ProxboxException, match="Error while syncing nodes."):
        asyncio.run(
            full_update_sync(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                custom_fields=[],
                tag=type("Tag", (), {"id": 1})(),
            )
        )


def test_full_update_sync_wraps_vm_phase_unexpected_errors(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.main.create_proxmox_devices",
        lambda **kwargs: asyncio.sleep(0, result=[{"id": 1}]),
    )

    async def _fail_vms(**kwargs):
        raise RuntimeError("vm exploded")

    monkeypatch.setattr("proxbox_api.main.create_virtual_machines", _fail_vms)

    with pytest.raises(ProxboxException, match="Error while syncing virtual machines."):
        asyncio.run(
            full_update_sync(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                custom_fields=[],
                tag=type("Tag", (), {"id": 1})(),
            )
        )


def test_create_virtual_machines_handles_empty_clusters_and_journal_failures():
    class FakeSyncProcess:
        id = 101
        status = "not-started"
        runtime = None

        def save(self):
            return None

    class FakeClient:
        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            if method == "POST" and path == "/api/plugins/proxbox/sync-processes/":
                return ApiResponse(
                    status=201,
                    text=json.dumps(
                        {
                            "id": 101,
                            "name": "sync-vms",
                            "sync_type": "virtual-machines",
                            "status": "not-started",
                            "started_at": "2025-03-13T15:08:09.051478Z",
                            "completed_at": None,
                            "runtime": None,
                            "url": "https://netbox.local/api/plugins/proxbox/sync-processes/101/",
                            "display": "sync-vms (virtual-machines)",
                        }
                    ),
                )
            if method == "PATCH" and path == "/api/plugins/proxbox/sync-processes/101/":
                body = {
                    "id": 101,
                    "name": "sync-vms",
                    "sync_type": "virtual-machines",
                    "status": payload.get("status", "completed"),
                    "started_at": "2025-03-13T15:08:09.051478Z",
                    "completed_at": payload.get("completed_at"),
                    "runtime": payload.get("runtime"),
                    "url": "https://netbox.local/api/plugins/proxbox/sync-processes/101/",
                    "display": "sync-vms (virtual-machines)",
                }
                return ApiResponse(status=200, text=json.dumps(body))
            raise AssertionError((method, path, query, payload, expect_json))

    class FakeJournalEntriesEndpoint:
        def create(self, payload):
            raise RuntimeError("journal create failed")

    fake_netbox = type(
        "FakeNetBoxSession",
        (),
        {
            "client": FakeClient(),
            "extras": type(
                "Extras",
                (),
                {"journal_entries": FakeJournalEntriesEndpoint()},
            )(),
        },
    )()

    result = asyncio.run(
        create_virtual_machines(
            netbox_session=fake_netbox,
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=type("Tag", (), {"id": 1})(),
        )
    )

    assert result == []


def test_create_netbox_backups_reuses_duplicate_backup(monkeypatch):
    class FakeVirtualMachineModel:
        def find(self, **kwargs):
            assert kwargs == {"cf_proxmox_vm_id": 101}
            return {"id": 55, "name": "vm101"}

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            self.calls.append((method, path, query, payload, expect_json))
            if method == "GET" and path == "/api/plugins/proxbox/backups/":
                if query == {"volume_id": "backup-store:vm/101/2026-03-29", "limit": 2}:
                    return ApiResponse(
                        status=200,
                        text=json.dumps(
                            {
                                "count": 0 if len(self.calls) == 1 else 1,
                                "results": (
                                    []
                                    if len(self.calls) == 1
                                    else [
                                        {
                                            "id": 900,
                                            "volume_id": "backup-store:vm/101/2026-03-29",
                                            "virtual_machine": 55,
                                            "storage": "backup-store",
                                        }
                                    ]
                                ),
                            }
                        ),
                    )
            if method == "POST" and path == "/api/plugins/proxbox/backups/":
                return ApiResponse(
                    status=400,
                    text=json.dumps({"volume_id": ["backup with this volume id already exists."]}),
                )
            raise AssertionError((method, path, query, payload, expect_json))

    journal_payloads = []

    fake_netbox = type(
        "FakeNetBoxSession",
        (),
        {
            "client": FakeClient(),
            "extras": type(
                "Extras",
                (),
                {
                    "journal_entries": type(
                        "JournalEntries",
                        (),
                        {"create": lambda self, payload: journal_payloads.append(payload)},
                    )()
                },
            )(),
        },
    )()

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.VirtualMachine",
        FakeVirtualMachineModel,
    )

    backup = asyncio.run(
        create_netbox_backups(
            {
                "vmid": "101",
                "volid": "backup-store:vm/101/2026-03-29",
                "subtype": "qemu",
                "format": "zst",
                "size": 1024,
                "ctime": 1711660800,
                "verification": {"state": "ok", "upid": "UPID:1"},
            },
            fake_netbox,
        )
    )

    assert backup.id == 900
    assert fake_netbox.client.calls == [
        (
            "GET",
            "/api/plugins/proxbox/backups/",
            {"volume_id": "backup-store:vm/101/2026-03-29", "limit": 2},
            None,
            True,
        ),
        (
            "POST",
            "/api/plugins/proxbox/backups/",
            None,
                {
                    "storage": "backup-store",
                    "virtual_machine": 55,
                    "subtype": "qemu",
                    "creation_time": datetime.fromtimestamp(1711660800).isoformat(),
                    "size": 1024,
                    "verification_state": "ok",
                    "verification_upid": "UPID:1",
                "volume_id": "backup-store:vm/101/2026-03-29",
                "notes": None,
                "vmid": "101",
                "format": "zst",
            },
            True,
        ),
        (
            "GET",
            "/api/plugins/proxbox/backups/",
            {"volume_id": "backup-store:vm/101/2026-03-29", "limit": 2},
            None,
            True,
        ),
    ]
    assert journal_payloads[0]["assigned_object_id"] == 900
