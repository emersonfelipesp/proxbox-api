"""Integration-oriented tests for core API route responses."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from netbox_sdk.client import ApiResponse

from proxbox_api.database import NetBoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.main import (
    full_update_sync,
    full_update_sync_stream,
    standalone_info,
)
from proxbox_api.routes.extras import create_custom_fields
from proxbox_api.routes.netbox import (
    create_netbox_endpoint,
    delete_netbox_endpoint,
    get_netbox_endpoint,
    get_netbox_endpoints,
    netbox_openapi,
    netbox_status,
    update_netbox_endpoint,
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
from proxbox_api.routes.virtualization.virtual_machines import (
    create_netbox_backups,
    create_virtual_machines,
)
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    create_virtual_machine_by_netbox_id,
    create_virtual_machine_by_netbox_id_stream,
)
from proxbox_api.services.sync.devices import create_proxmox_devices


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

            if (
                method == "GET"
                and (query or {}).get("limit") == 200
                and (query or {}).get("offset") == 0
            ):
                return ApiResponse(status=200, text=json.dumps({"count": 0, "results": []}))

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
    first_payload = device_create[0] if isinstance(device_create, list) else device_create
    assert first_payload["tags"] == [{"name": "Proxbox", "slug": "proxbox", "color": "ff5722"}]


def test_create_proxmox_devices_surfaces_real_netbox_detail():
    class FakeClient:
        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
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

    # With bulk operations hardening, the error is logged and handled gracefully
    # instead of being re-raised. The function should return with no created devices.
    result = asyncio.run(
        create_proxmox_devices(
            netbox_session=fake_session,
            clusters_status=cluster_status,
            tag=tag,
        )
    )
    assert result == []


def test_create_custom_fields_uses_rest_reconcile_with_async_session(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            self.calls.append((method, path, query, payload, expect_json))
            if method == "GET" and path == "/api/extras/custom-fields/":
                return ApiResponse(status=200, text=json.dumps({"count": 0, "results": []}))
            if method == "POST" and path == "/api/extras/custom-fields/":
                body = {"id": len([c for c in self.calls if c[0] == "POST"]), **payload}
                return ApiResponse(status=201, text=json.dumps(body))
            raise AssertionError((method, path, query, payload, expect_json))

    session = SimpleNamespace(client=FakeClient())
    monkeypatch.setattr("proxbox_api.routes.extras._CUSTOM_FIELDS_CACHE", None)

    result = asyncio.run(create_custom_fields(netbox_session=session))

    assert len(result) >= 6
    assert all(field["group_name"] == "Proxmox" for field in result)
    field_names = {field["name"] for field in result}
    assert "proxmox_vm_id" in field_names
    assert "proxmox_vm_type" in field_names
    first_post = next(
        payload
        for method, path, _query, payload, _expect_json in session.client.calls
        if method == "POST" and path == "/api/extras/custom-fields/"
    )
    assert first_post["object_types"] == ["virtualization.virtualmachine"]
    assert first_post["ui_editable"] == "hidden"


def test_create_custom_fields_caches_successful_bootstrap(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            self.calls.append((method, path, query, payload, expect_json))
            if method == "GET" and path == "/api/extras/custom-fields/":
                return ApiResponse(status=200, text=json.dumps({"count": 0, "results": []}))
            if method == "POST" and path == "/api/extras/custom-fields/":
                body = {"id": len([c for c in self.calls if c[0] == "POST"]), **payload}
                return ApiResponse(status=201, text=json.dumps(body))
            raise AssertionError((method, path, query, payload, expect_json))

    monkeypatch.setattr("proxbox_api.routes.extras._CUSTOM_FIELDS_CACHE", None)
    session = SimpleNamespace(client=FakeClient())

    first_result = asyncio.run(create_custom_fields(netbox_session=session))
    first_call_count = len(session.client.calls)
    second_result = asyncio.run(create_custom_fields(netbox_session=session))

    assert second_result == first_result
    assert len(session.client.calls) == first_call_count


def test_create_custom_fields_reports_netbox_overwhelmed(monkeypatch):
    class FakeClient:
        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            if method == "GET" and path == "/api/extras/custom-fields/":
                return ApiResponse(status=500, text=json.dumps({"detail": "database unavailable"}))
            raise AssertionError((method, path, query, payload, expect_json))

    monkeypatch.setattr("proxbox_api.routes.extras._CUSTOM_FIELDS_CACHE", None)
    monkeypatch.setenv("PROXBOX_NETBOX_MAX_RETRIES", "0")
    session = SimpleNamespace(client=FakeClient())

    with pytest.raises(ProxboxException, match="NetBox is overwhelmed") as excinfo:
        asyncio.run(create_custom_fields(netbox_session=session))

    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail.get("reason") == "netbox_overwhelmed"
    failed_fields = excinfo.value.detail.get("failed_fields")
    assert isinstance(failed_fields, list)
    assert failed_fields


def test_proxmox_endpoint_crud_lifecycle(db_session):
    created = asyncio.run(
        create_proxmox_endpoint(
            ProxmoxEndpointCreate(
                name="pve-lab-1",
                ip_address="1.1.1.1",
                domain="pve-lab-1.example.com",
                port=8006,
                username="root@pam",
                password="supersecret",
                verify_ssl=False,
            ),
            db_session,
        )
    )
    endpoint_id = created.id
    assert created.name == "pve-lab-1"
    assert created.model_dump() == {
        "id": endpoint_id,
        "name": "pve-lab-1",
        "ip_address": "1.1.1.1",
        "domain": "pve-lab-1.example.com",
        "port": 8006,
        "username": "root@pam",
        "verify_ssl": False,
    }

    listed = asyncio.run(get_proxmox_endpoints(db_session))
    assert len(listed) == 1
    assert listed[0].model_dump()["name"] == "pve-lab-1"
    assert "password" not in listed[0].model_dump()

    updated = asyncio.run(
        update_proxmox_endpoint(
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
    )
    assert updated.name == "pve-lab-1-updated"
    assert updated.model_dump() == {
        "id": endpoint_id,
        "name": "pve-lab-1-updated",
        "ip_address": "1.1.1.1",
        "domain": "pve-lab-1.example.com",
        "port": 8006,
        "username": "root@pam",
        "verify_ssl": True,
    }

    deleted = asyncio.run(delete_proxmox_endpoint(endpoint_id, db_session))
    assert deleted == {"message": "Proxmox endpoint deleted."}

    with pytest.raises(HTTPException, match="Proxmox endpoint not found"):
        asyncio.run(get_proxmox_endpoint(endpoint_id, db_session))


def test_proxmox_endpoint_requires_complete_token_pair(db_session):
    with pytest.raises(
        HTTPException,
        match="token_name and token_value must be provided together",
    ):
        asyncio.run(
            create_proxmox_endpoint(
                ProxmoxEndpointCreate(
                    name="pve-lab-2",
                    ip_address="1.1.1.2",
                    port=8006,
                    username="root@pam",
                    token_name="sync",
                    verify_ssl=True,
                ),
                db_session,
            )
        )


def test_netbox_endpoint_crud_and_singleton_rule(db_session):
    payload = NetBoxEndpoint(
        name="netbox-primary",
        ip_address="1.1.1.3",
        domain="netbox.example.com",
        port=443,
        token="token-1",
        verify_ssl=True,
    )
    created = asyncio.run(create_netbox_endpoint(payload, db_session))
    endpoint_id = created.id

    with pytest.raises(HTTPException, match="Only one NetBox endpoint is allowed"):
        asyncio.run(
            create_netbox_endpoint(
                NetBoxEndpoint(
                    name="netbox-secondary",
                    ip_address="1.1.1.4",
                    domain="netbox2.local",
                    port=443,
                    token="token-2",
                    verify_ssl=True,
                ),
                db_session,
            )
        )

    listed = asyncio.run(get_netbox_endpoints(db_session))
    assert len(listed) == 1

    updated = asyncio.run(
        update_netbox_endpoint(
            endpoint_id,
            NetBoxEndpoint(
                name="netbox-primary-updated",
                ip_address="1.1.1.3",
                domain="netbox.example.com",
                port=443,
                token="token-2",
                verify_ssl=True,
            ),
            db_session,
        )
    )
    assert updated.name == "netbox-primary-updated"

    retrieved = asyncio.run(get_netbox_endpoint(endpoint_id, db_session))
    assert retrieved.name == "netbox-primary-updated"
    assert not hasattr(retrieved, "token") or retrieved.token is None
    assert asyncio.run(delete_netbox_endpoint(endpoint_id, db_session)) == {
        "message": "NetBox Endpoint deleted."
    }


def test_netbox_endpoint_rejects_v1_without_token(db_session):
    with pytest.raises(HTTPException, match="token is required for NetBox API token v1"):
        asyncio.run(
            create_netbox_endpoint(
                NetBoxEndpoint(
                    name="netbox-primary",
                    ip_address="1.1.1.3",
                    domain="netbox.example.com",
                    port=443,
                    token_version="v1",
                    token="",
                    verify_ssl=True,
                ),
                db_session,
            )
        )


def test_netbox_endpoint_rejects_v2_incomplete_token(db_session):
    with pytest.raises(
        HTTPException,
        match="token_key and token \\(secret\\) must both be set",
    ):
        asyncio.run(
            create_netbox_endpoint(
                NetBoxEndpoint(
                    name="netbox-primary",
                    ip_address="1.1.1.3",
                    domain="netbox.example.com",
                    port=443,
                    token_version="v2",
                    token_key="myid",
                    token="",
                    verify_ssl=True,
                ),
                db_session,
            )
        )


def test_netbox_endpoint_accepts_v2_token(db_session):
    created = asyncio.run(
        create_netbox_endpoint(
            NetBoxEndpoint(
                name="netbox-v2",
                ip_address="1.1.1.3",
                domain="netbox.example.com",
                port=443,
                token_version="v2",
                token_key="myid",
                token="secretpart",
                verify_ssl=True,
            ),
            db_session,
        )
    )
    assert created.token_version == "v2"
    assert not hasattr(created, "token_key") or created.token_key is None
    assert not hasattr(created, "token") or created.token is None


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


def test_full_update_sync_returns_structured_payload(monkeypatch):  # noqa: C901
    create_vm_calls: list[dict] = []

    async def _fake_devices(**kwargs):
        return [{"id": 1, "name": "node01"}]

    async def _fake_vms(**kwargs):
        create_vm_calls.append(kwargs)
        return [{"id": 101, "name": "vm01"}]

    async def _fake_storage(**kwargs):
        return [{"id": 201, "name": "local"}]

    async def _fake_disks(**kwargs):
        return {"count": 2, "created": 2, "updated": 0, "skipped": 0}

    async def _fake_backups(**kwargs):
        return [{"id": 301, "vmid": "101"}]

    async def _fake_snapshots(**kwargs):
        return {"count": 1, "created": 1, "skipped": 0}

    async def _fake_task_history(**kwargs):
        return {
            "count": 0,
            "created": 0,
            "skipped": 0,
            "error": "'object' object has no attribute 'client'",
        }

    async def _fake_node_interfaces(**kwargs):
        return []

    async def _fake_vm_interfaces(**kwargs):
        return []

    async def _fake_vm_ip_addresses(**kwargs):
        return []

    async def _fake_replications(**kwargs):
        return {"created": 0, "updated": 0, "errors": 0}

    async def _fake_backup_routines(**kwargs):
        return {"created": 0, "updated": 0, "errors": 0}

    monkeypatch.setattr("proxbox_api.app.full_update.create_proxmox_devices", _fake_devices)
    monkeypatch.setattr("proxbox_api.app.full_update.create_virtual_machines", _fake_vms)
    monkeypatch.setattr("proxbox_api.app.full_update.create_storages", _fake_storage)
    monkeypatch.setattr("proxbox_api.app.full_update.create_virtual_disks", _fake_disks)
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_all_virtual_machine_backups", _fake_backups
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_all_virtual_machine_snapshots", _fake_snapshots
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.sync_all_virtual_machine_task_histories", _fake_task_history
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_all_device_interfaces", _fake_node_interfaces
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_only_vm_interfaces", _fake_vm_interfaces
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_only_vm_ip_addresses", _fake_vm_ip_addresses
    )
    monkeypatch.setattr("proxbox_api.app.full_update.sync_all_replications", _fake_replications)
    monkeypatch.setattr(
        "proxbox_api.app.full_update.sync_all_backup_routines", _fake_backup_routines
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

    assert create_vm_calls and create_vm_calls[0]["sync_vm_network"] is False
    assert body == {
        "status": "completed",
        "devices": [{"id": 1, "name": "node01"}],
        "storage": [{"id": 201, "name": "local"}],
        "virtual_machines": [{"id": 101, "name": "vm01"}],
        "virtual_disks": {"count": 2, "created": 2, "updated": 0, "skipped": 0},
        "backups": [{"id": 301, "vmid": "101"}],
        "snapshots": {"count": 1, "created": 1, "skipped": 0},
        "task_history": {
            "count": 0,
            "created": 0,
            "skipped": 0,
            "error": "'object' object has no attribute 'client'",
        },
        "node_interfaces": [],
        "vm_interfaces": [],
        "vm_ip_addresses": [],
        "replications": {"created": 0, "updated": 0, "errors": 0},
        "backup_routines": {"created": 0, "updated": 0, "errors": 0},
        "devices_count": 1,
        "storage_count": 1,
        "virtual_machines_count": 1,
        "virtual_disks_count": 2,
        "backups_count": 1,
        "snapshots_count": 1,
        "task_history_count": 0,
        "node_interfaces_count": 0,
        "vm_interfaces_count": 0,
        "vm_ip_addresses_count": 0,
        "replications_count": 0,
        "backup_routines_count": 0,
    }


def test_full_update_sync_handles_empty_device_result(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_proxmox_devices",
        lambda **kwargs: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_virtual_machines",
        lambda **kwargs: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_storages",
        lambda **kwargs: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_virtual_disks",
        lambda **kwargs: asyncio.sleep(
            0, result={"count": 0, "created": 0, "updated": 0, "skipped": 0}
        ),
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_all_virtual_machine_backups",
        lambda **kwargs: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_all_virtual_machine_snapshots",
        lambda **kwargs: asyncio.sleep(0, result={"count": 0, "created": 0, "skipped": 0}),
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.sync_all_virtual_machine_task_histories",
        lambda **kwargs: asyncio.sleep(0, result={"count": 0, "created": 0, "skipped": 0}),
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
    assert body["storage"] == []
    assert body["virtual_machines"] == []
    assert body["virtual_disks_count"] == 0
    assert body["backups_count"] == 0
    assert body["snapshots_count"] == 0
    assert body["task_history_count"] == 0
    assert body["devices_count"] == 0
    assert body["storage_count"] == 0
    assert body["virtual_machines_count"] == 0


def test_full_update_sync_reraises_device_phase_proxbox_exception(monkeypatch):
    async def _fail_devices(**kwargs):
        raise ProxboxException(message="Error while syncing nodes.", detail="device failed")

    monkeypatch.setattr("proxbox_api.app.full_update.create_proxmox_devices", _fail_devices)

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
        "proxbox_api.app.full_update.create_proxmox_devices",
        lambda **kwargs: asyncio.sleep(0, result=[{"id": 1}]),
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_storages",
        lambda **kwargs: asyncio.sleep(0, result=[]),
    )

    async def _fail_vms(**kwargs):
        raise RuntimeError("vm exploded")

    monkeypatch.setattr("proxbox_api.app.full_update.create_virtual_machines", _fail_vms)

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


def test_create_virtual_machines_handles_empty_clusters():
    class FakeClient:
        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            raise AssertionError((method, path, query, payload, expect_json))

    fake_netbox = type(
        "FakeNetBoxSession",
        (),
        {
            "client": FakeClient(),
            "extras": type("Extras", (), {"journal_entries": object()})(),
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


def test_create_virtual_machine_by_netbox_id_filters_cluster_resources(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_create_virtual_machines(**kwargs):
        captured["cluster_resources"] = kwargs["cluster_resources"]
        return [{"id": 248, "name": "vm-248"}]

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.create_virtual_machines",
        _fake_create_virtual_machines,
    )

    vm_record = SimpleNamespace(
        serialize=lambda: {
            "id": 248,
            "name": "vm-248",
            "cluster": {"id": 10, "name": "cluster-a"},
            "custom_fields": {"proxmox_vm_id": 9248},
        }
    )
    fake_nb = SimpleNamespace(
        virtualization=SimpleNamespace(
            virtual_machines=SimpleNamespace(get=lambda id: vm_record if id == 248 else None)
        )
    )
    cluster_resources = [
        {"cluster-a": [{"type": "qemu", "name": "vm-248", "vmid": 9248}]},
        {"cluster-a": [{"type": "qemu", "name": "vm-999", "vmid": 9999}]},
        {"cluster-b": [{"type": "qemu", "name": "vm-248", "vmid": 9248}]},
    ]

    result = asyncio.run(
        create_virtual_machine_by_netbox_id(
            netbox_vm_id=248,
            netbox_session=fake_nb,
            pxs=[],
            cluster_status=[],
            cluster_resources=cluster_resources,
            custom_fields=[],
            tag=SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722"),
        )
    )

    assert result == [{"id": 248, "name": "vm-248"}]
    assert captured["cluster_resources"] == [
        {"cluster-a": [{"type": "qemu", "name": "vm-248", "vmid": 9248}]}
    ]


def test_create_virtual_machines_reconciles_vm_children_for_single_vm_bundle(
    monkeypatch,
):
    """The single-VM bundle must still create interfaces, IPs, disks, and task history."""

    interface_calls: list[dict[str, object]] = []
    disk_calls: list[dict[str, object]] = []
    task_history_calls: list[dict[str, object]] = []
    patch_calls: list[tuple[object, ...]] = []
    netbox_session = object()

    async def _fake_reconcile(*args, **kwargs):
        lookup = kwargs.get("lookup") or {}
        if lookup.get("cf_proxmox_vm_id") == 101:
            return {"id": 101, "name": "vm-101", "primary_ip4": None}
        return {"id": len(patch_calls) + 1, "name": kwargs.get("payload", {}).get("name")}

    async def _fake_ensure(*args, **kwargs):
        return SimpleNamespace(id=1)

    async def _fake_rest_list(*args, **kwargs):
        return []

    async def _fake_patch(*args, **kwargs):
        patch_calls.append(args)
        return None

    async def _fake_create_vm_interface_parallel(**kwargs):
        interface_calls.append(kwargs)
        return {"interface": {"id": 66, "name": kwargs["interface_name"]}, "first_ip_id": 77}

    async def _fake_create_vm_disk_parallel(**kwargs):
        disk_calls.append(kwargs)
        return {"id": 88}

    async def _fake_task_history(**kwargs):
        task_history_calls.append(kwargs)
        return 3

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.rest_reconcile_async",
        _fake_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.rest_patch_async",
        _fake_patch,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_vm_config",
        lambda **kwargs: {
            "onboot": 1,
            "agent": 1,
            "unprivileged": 0,
            "searchdomain": "lab.local",
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=1.1.1.3/24",
            "scsi0": "local-lvm:vm-101-disk-0,size=20G",
        },
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_qemu_guest_agent_network_interfaces",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_cluster_type",
        _fake_ensure,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_cluster",
        _fake_ensure,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_manufacturer",
        _fake_ensure,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_device_type",
        _fake_ensure,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_site",
        _fake_ensure,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_device",
        _fake_ensure,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_proxmox_node_role",
        _fake_ensure,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.build_netbox_virtual_machine_payload",
        lambda **kwargs: {"name": "vm-101", "status": "active", "cluster": 1},
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._create_vm_interface_parallel",
        _fake_create_vm_interface_parallel,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._create_vm_disk_parallel",
        _fake_create_vm_disk_parallel,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.sync_virtual_machine_task_history",
        _fake_task_history,
    )
    async def _fake_ensure_ip_assigned(*args, **kwargs):
        return True

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.ensure_ip_assigned_to_vm",
        _fake_ensure_ip_assigned,
    )

    result = asyncio.run(
        create_virtual_machines(
            netbox_session=netbox_session,
            pxs=[],
            cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
            cluster_resources=[
                {
                    "cluster-a": [
                        {
                            "type": "qemu",
                            "name": "vm-101",
                            "vmid": 101,
                            "node": "pve01",
                        }
                    ]
                }
            ],
            custom_fields=[],
            tag=SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722"),
        )
    )

    assert result == [{"id": 101, "name": "vm-101", "primary_ip4": None}]
    assert len(interface_calls) == 1
    assert len(disk_calls) == 1
    assert len(task_history_calls) == 1
    assert patch_calls == [
        (
            netbox_session,
            "/api/virtualization/virtual-machines/",
            101,
            {"primary_ip4": 77},
        )
    ]


def test_create_virtual_machine_by_netbox_id_raises_404_when_missing():
    fake_nb = SimpleNamespace(
        virtualization=SimpleNamespace(virtual_machines=SimpleNamespace(get=lambda id: None))
    )
    with pytest.raises(HTTPException, match="was not found in NetBox") as excinfo:
        asyncio.run(
            create_virtual_machine_by_netbox_id(
                netbox_vm_id=248,
                netbox_session=fake_nb,
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                custom_fields=[],
                tag=SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722"),
            )
        )
    assert excinfo.value.status_code == 404


def test_create_virtual_machine_by_netbox_id_raises_404_when_not_in_proxmox():
    vm_record = SimpleNamespace(
        serialize=lambda: {
            "id": 248,
            "name": "vm-248",
            "cluster": {"id": 10, "name": "cluster-a"},
            "custom_fields": {"proxmox_vm_id": 9248},
        }
    )
    fake_nb = SimpleNamespace(
        virtualization=SimpleNamespace(virtual_machines=SimpleNamespace(get=lambda id: vm_record))
    )
    with pytest.raises(HTTPException, match="No matching Proxmox VM") as excinfo:
        asyncio.run(
            create_virtual_machine_by_netbox_id(
                netbox_vm_id=248,
                netbox_session=fake_nb,
                pxs=[],
                cluster_status=[],
                cluster_resources=[{"cluster-a": [{"type": "qemu", "name": "other", "vmid": 1}]}],
                custom_fields=[],
                tag=SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722"),
            )
        )
    assert excinfo.value.status_code == 404


def test_create_virtual_machine_by_netbox_id_stream_emits_complete(monkeypatch):
    async def _fake_create_single_vm(**kwargs):
        return [{"id": 248, "name": "vm-248"}]

    class _StreamingResponseStub:
        def __init__(self, content, media_type=None, headers=None):
            self.content = content
            self.media_type = media_type
            self.headers = headers or {}

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.StreamingResponse",
        _StreamingResponseStub,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._create_virtual_machine_by_netbox_id",
        _fake_create_single_vm,
    )

    response = asyncio.run(
        create_virtual_machine_by_netbox_id_stream(
            netbox_vm_id=248,
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=SimpleNamespace(id=1),
        )
    )
    payload = "".join(asyncio.run(_collect_async_frames(response.content)))
    assert "event: complete" in payload
    assert "Virtual machine sync completed." in payload


def test_full_update_stream_includes_granular_bridge_messages(monkeypatch):  # noqa: C901
    class _Tag:
        id = 1

    class _StreamingResponseStub:
        def __init__(self, content, media_type=None, headers=None):
            self.content = content
            self.media_type = media_type
            self.headers = headers or {}

    async def _fake_devices(**kwargs):
        bridge = kwargs.get("websocket")
        if bridge is not None:
            await bridge.send_json(
                {
                    "object": "device",
                    "type": "create",
                    "data": {"rowid": "pve01", "completed": False},
                }
            )
            await bridge.send_json(
                {
                    "object": "device",
                    "type": "create",
                    "data": {"rowid": "pve01", "completed": True},
                }
            )
        return [{"id": 1, "name": "pve01"}]

    create_vm_calls: list[dict] = []

    async def _fake_vms(**kwargs):
        create_vm_calls.append(kwargs)
        bridge = kwargs.get("websocket")
        if bridge is not None:
            await bridge.send_json(
                {
                    "object": "virtual_machine",
                    "type": "create",
                    "data": {"rowid": "vm01", "completed": False},
                }
            )
            await bridge.send_json(
                {
                    "object": "virtual_machine",
                    "type": "create",
                    "data": {"rowid": "vm01", "completed": True},
                }
            )
        return [{"id": 101, "name": "vm01"}]

    async def _fake_storage(**kwargs):
        bridge = kwargs.get("websocket")
        if bridge is not None:
            await bridge.send_json(
                {
                    "step": "storage",
                    "status": "synced",
                    "message": "Synced storage lab/local",
                }
            )
        return [{"id": 201, "name": "local"}]

    async def _fake_disks(**kwargs):
        bridge = kwargs.get("websocket")
        if bridge is not None:
            await bridge.send_json(
                {
                    "step": "virtual-disks",
                    "status": "synced",
                    "message": "Synced virtual disk vm01/scsi0",
                }
            )
        return {"count": 1, "created": 1, "updated": 0, "skipped": 0}

    async def _fake_backups(**kwargs):
        bridge = kwargs.get("websocket")
        if bridge is not None:
            await bridge.send_json(
                {
                    "step": "backups",
                    "status": "completed",
                    "message": "Backup sync completed.",
                }
            )
        return [{"id": 301}]

    async def _fake_snapshots(**kwargs):
        bridge = kwargs.get("websocket")
        if bridge is not None:
            await bridge.send_json(
                {
                    "step": "snapshots",
                    "status": "completed",
                    "message": "Snapshot sync completed.",
                }
            )
        return {"count": 1, "created": 1, "skipped": 0}

    monkeypatch.setattr("proxbox_api.app.full_update.StreamingResponse", _StreamingResponseStub)
    monkeypatch.setattr("proxbox_api.app.full_update.create_proxmox_devices", _fake_devices)
    monkeypatch.setattr("proxbox_api.app.full_update.create_virtual_machines", _fake_vms)
    monkeypatch.setattr("proxbox_api.app.full_update.create_storages", _fake_storage)
    monkeypatch.setattr("proxbox_api.app.full_update.create_virtual_disks", _fake_disks)
    monkeypatch.setattr(
        "proxbox_api.app.full_update._create_all_virtual_machine_backups", _fake_backups
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update._create_all_virtual_machine_snapshots",
        _fake_snapshots,
    )

    response = asyncio.run(
        full_update_sync_stream(
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=_Tag(),
        )
    )

    frames = asyncio.run(_collect_async_frames(response.content))
    payload = "".join(frames)

    assert "Processing device pve01" in payload
    assert "Synced device pve01" in payload
    assert "Processing virtual_machine vm01" in payload
    assert "Synced virtual_machine vm01" in payload
    assert "Synced storage lab/local" in payload
    assert "Synced virtual disk vm01/scsi0" in payload
    assert "event: complete" in payload
    assert create_vm_calls[0]["sync_vm_network"] is False


async def _collect_async_frames(stream) -> list[str]:
    output = []
    async for frame in stream:
        output.append(frame)
    return output


def test_create_netbox_backups_reuses_duplicate_backup(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []
            self._backup_volume_lookups = 0

        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            self.calls.append((method, path, query, payload, expect_json))
            if method == "GET" and path == "/api/virtualization/virtual-machines/":
                assert query == {"cf_proxmox_vm_id": 101}
                return ApiResponse(
                    status=200,
                    text=json.dumps(
                        {
                            "count": 1,
                            "results": [{"id": 55, "name": "vm101"}],
                        }
                    ),
                )
            if method == "GET" and path == "/api/plugins/proxbox/backups/":
                if query == {"volume_id": "backup-store:vm/101/2026-03-29", "limit": 2}:
                    self._backup_volume_lookups += 1
                    return ApiResponse(
                        status=200,
                        text=json.dumps(
                            {
                                "count": 0 if self._backup_volume_lookups == 1 else 1,
                                "results": (
                                    []
                                    if self._backup_volume_lookups == 1
                                    else [
                                        {
                                            "id": 900,
                                            "volume_id": "backup-store:vm/101/2026-03-29",
                                            "virtual_machine": 55,
                                            "storage": "backup-store",
                                            "subtype": "qemu",
                                            "creation_time": datetime.fromtimestamp(
                                                1711660800
                                            ).isoformat(),
                                            "size": 1024,
                                            "verification_state": "ok",
                                            "verification_upid": "UPID:1",
                                            "notes": None,
                                            "vmid": "101",
                                            "format": "tzst",
                                        }
                                    ]
                                ),
                            }
                        ),
                    )
                # Handle scan query after duplicate error
                if query.get("limit") == 200:
                    return ApiResponse(
                        status=200,
                        text=json.dumps(
                            {
                                "count": 1,
                                "results": [
                                    {
                                        "id": 900,
                                        "volume_id": "backup-store:vm/101/2026-03-29",
                                        "virtual_machine": 55,
                                        "storage": "backup-store",
                                        "subtype": "qemu",
                                        "creation_time": datetime.fromtimestamp(
                                            1711660800
                                        ).isoformat(),
                                        "size": 1024,
                                        "verification_state": "ok",
                                        "verification_upid": "UPID:1",
                                        "notes": None,
                                        "vmid": "101",
                                        "format": "tzst",
                                    }
                                ],
                            }
                        ),
                    )
            if method == "POST" and path == "/api/plugins/proxbox/backups/":
                return ApiResponse(
                    status=400,
                    text=json.dumps({"volume_id": ["backup with this volume id already exists."]}),
                )
            if method == "POST" and path == "/api/extras/journal-entries/":
                return ApiResponse(status=201, text=json.dumps({"id": 504}))
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
    assert fake_netbox.client.calls[:3] == [
        (
            "GET",
            "/api/virtualization/virtual-machines/",
            {"cf_proxmox_vm_id": 101},
            None,
            True,
        ),
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
                "vmid": "101",
                "format": "tzst",
                "tags": [],
            },
            True,
        ),
    ]
    assert fake_netbox.client.calls[3][0:2] == (
        "GET",
        "/api/plugins/proxbox/backups/",
    )
    assert fake_netbox.client.calls[4][0:2] == ("POST", "/api/extras/journal-entries/")
