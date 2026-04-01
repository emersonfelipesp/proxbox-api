from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from netbox_sdk.client import ApiResponse
from sqlmodel import Session

from proxbox_api.database import NetBoxEndpoint, ProxmoxEndpoint
from proxbox_api.dependencies import proxbox_tag
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import (
    ensure_tag_async,
    rest_create,
    rest_ensure_async,
    rest_reconcile_async,
)
from proxbox_api.netbox_sdk_helpers import ensure_record, ensure_tag, to_dict
from proxbox_api.netbox_sdk_sync import SyncProxy
from proxbox_api.proxmox_to_netbox.models import NetBoxDeviceSyncState, NetBoxSiteSyncState
from proxbox_api.routes.proxmox import get_proxmox_node_storage_content, get_vm_config
from proxbox_api.routes.proxmox.cluster import cluster_resources, cluster_status
from proxbox_api.services.proxmox_helpers import (
    get_cluster_resources as get_typed_cluster_resources,
)
from proxbox_api.services.proxmox_helpers import (
    get_cluster_status as get_typed_cluster_status,
)
from proxbox_api.services.proxmox_helpers import (
    get_node_storage_content as get_typed_node_storage_content,
)
from proxbox_api.services.proxmox_helpers import (
    get_storage_list as get_typed_storage_list,
)
from proxbox_api.services.proxmox_helpers import (
    get_vm_config as get_typed_vm_config,
)
from proxbox_api.session import netbox as netbox_session_module
from proxbox_api.session.netbox import get_netbox_async_session
from proxbox_api.session.proxmox import ProxmoxSession, proxmox_sessions


class AsyncEndpoint:
    def __init__(self, *, existing=None, created=None):
        self.existing = existing
        self.created = created or {"id": 99}
        self.created_payload = None
        self.get_calls = []

    async def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return self.existing

    async def create(self, payload):
        self.created_payload = payload
        return self.created


class AsyncTagsEndpoint(AsyncEndpoint):
    pass


class FailingAsyncTagsEndpoint(AsyncEndpoint):
    async def get(self, **kwargs):
        return None

    async def create(self, payload):
        raise RuntimeError("tag create failed")


class AsyncNetBoxFacade:
    def __init__(self):
        self.status_calls = 0
        self.extras = SimpleNamespace(tags=AsyncTagsEndpoint())

    async def status(self):
        self.status_calls += 1
        return {"netbox": "ok"}


class AsyncFailingTagFacade:
    def __init__(self):
        self.extras = SimpleNamespace(tags=FailingAsyncTagsEndpoint())


class AsyncIterableEndpoint:
    def __init__(self, items):
        self._items = items

    async def all(self):
        for item in self._items:
            yield item


class FauxAsyncNamespace:
    child = SimpleNamespace(value=1)

    def __getattr__(self, name):
        if name == "__aiter__":
            return object()
        raise AttributeError(name)


class RestClientStub:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    async def request(self, method, path, *, query=None, payload=None, expect_json=True):
        self.calls.append((method, path, query, payload, expect_json))
        key = (method, path)
        response = self._responses[key]
        if callable(response):
            response = response(query, payload)
        status, body = response
        text = body if isinstance(body, str) else json.dumps(body)
        return ApiResponse(status=status, text=text, headers={"Content-Type": "application/json"})


class AsyncNetBoxRestFacade:
    def __init__(self, responses):
        self.client = RestClientStub(responses)


class FakeProxmoxResource:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *args, **kwargs):
        return self.payload


class FakeProxmoxAPI:
    def __init__(self, host, **kwargs):
        self.host = host
        self.kwargs = kwargs
        self.version = FakeProxmoxResource({"version": "8.3.0"})

    def __call__(self, path):
        if path == "cluster/status":
            return FakeProxmoxResource(
                [
                    {"type": "cluster", "name": "lab-cluster"},
                    {"type": "node", "name": "pve01"},
                ]
            )
        if path == "cluster/config/join":
            return FakeProxmoxResource({"nodelist": [{"pve_fp": "fingerprint"}]})
        raise AssertionError(f"unexpected path {path}")


class FakeFailThenSucceedProxmoxAPI(FakeProxmoxAPI):
    domain_attempts = 0

    def __init__(self, host, **kwargs):
        if host == "pve.local":
            type(self).domain_attempts += 1
            raise RuntimeError("domain failed")
        super().__init__(host, **kwargs)


class FakePermissionDeniedVersionResource(FakeProxmoxResource):
    pass


class FakePermissionDeniedClusterResource(FakeProxmoxResource):
    def get(self, *args, **kwargs):
        raise RuntimeError("403 Forbidden: Permission check failed (/, Sys.Audit)")


class FakePermissionDeniedProxmoxAPI:
    def __init__(self, host, **kwargs):
        self.host = host
        self.kwargs = kwargs
        self.version = FakePermissionDeniedVersionResource({"version": "8.3.0"})

    def __call__(self, path):
        if path == "cluster/status":
            return FakePermissionDeniedClusterResource(None)
        raise AssertionError(f"unexpected path {path}")


class FakeNestedResource:
    def __init__(self, payload):
        self._payload = payload

    def get(self, **kwargs):
        return self._payload


class FakeStorageContentAccessor:
    def __init__(self, payload):
        self.content = FakeNestedResource(payload)


class FakeNodeVmAccessor:
    def __init__(self, payload):
        self.config = FakeNestedResource(payload)


class FakeNodeAccessor:
    def __init__(self, storage_content_payload, qemu_config_payload, lxc_config_payload):
        self._storage_content_payload = storage_content_payload
        self._qemu_config_payload = qemu_config_payload
        self._lxc_config_payload = lxc_config_payload

    def storage(self, storage):
        assert storage == "local"
        return FakeStorageContentAccessor(self._storage_content_payload)

    def qemu(self, vmid):
        assert vmid == 101
        return FakeNodeVmAccessor(self._qemu_config_payload)

    def lxc(self, vmid):
        assert vmid == 102
        return FakeNodeVmAccessor(self._lxc_config_payload)


class FakeTypedSessionAPI:
    def __init__(self):
        self.storage = FakeNestedResource([{"storage": "local"}])

    def __call__(self, path):
        if path == "cluster/status":
            return FakeNestedResource(
                [
                    {
                        "id": "cluster/lab",
                        "name": "lab",
                        "type": "cluster",
                        "nodes": 1,
                        "quorate": True,
                        "version": 7,
                    },
                    {
                        "id": "node/pve01",
                        "name": "pve01",
                        "type": "node",
                        "ip": "10.0.0.10",
                        "local": True,
                        "nodeid": 1,
                        "online": True,
                    },
                ]
            )
        if path == "cluster/resources":
            return FakeNestedResource(
                [
                    {
                        "id": "qemu/101",
                        "name": "vm01",
                        "node": "pve01",
                        "type": "qemu",
                        "status": "running",
                        "vmid": 101,
                    }
                ]
            )
        raise AssertionError(f"unexpected path {path}")

    def nodes(self, node):
        assert node == "pve01"
        return FakeNodeAccessor(
            storage_content_payload=[
                {
                    "format": "tgz",
                    "size": 2048,
                    "volid": "local:backup/vzdump-qemu-101.vma.zst",
                    "content": "backup",
                    "vmid": 101,
                }
            ],
            qemu_config_payload={
                "digest": "abc123",
                "name": "vm01",
                "cores": 2,
                "memory": "4096",
                "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
            },
            lxc_config_payload={
                "arch": "amd64",
                "cores": 1,
                "hostname": "ct01",
                "memory": 1024,
            },
        )


class FakeTypedProxmoxSession:
    def __init__(self):
        self.name = "lab"
        self.mode = "cluster"
        self.session = FakeTypedSessionAPI()


@dataclass
class SerializableRecord:
    id: int

    def serialize(self):
        return {"id": self.id}


def test_to_dict_supports_dict_and_serializable_objects():
    assert to_dict({"id": 1}) == {"id": 1}
    assert to_dict(SerializableRecord(id=2)) == {"id": 2}
    assert to_dict(object()) == {}


def test_ensure_record_get_or_create_behavior():
    existing_endpoint = AsyncEndpoint(existing={"id": 10})
    created_endpoint = AsyncEndpoint(existing=None, created={"id": 11})

    existing = asyncio.run(ensure_record(existing_endpoint, {"name": "vm01"}, {"name": "vm01"}))
    created = asyncio.run(ensure_record(created_endpoint, {"name": "vm02"}, {"name": "vm02"}))

    assert existing == {"id": 10}
    assert created == {"id": 11}
    assert created_endpoint.created_payload == {"name": "vm02"}


def test_ensure_record_reuses_duplicate_resource_via_payload_fallback():
    class DuplicateThenNameLookupEndpoint(AsyncEndpoint):
        async def get(self, **kwargs):
            self.get_calls.append(kwargs)
            if kwargs.get("name") == "Proxmox Node":
                return {"id": 44, "name": "Proxmox Node", "slug": "proxmox-node"}
            return None

        async def create(self, payload):
            self.created_payload = payload
            raise RuntimeError('{"name":["already exists"]}')

    endpoint = DuplicateThenNameLookupEndpoint()

    existing = asyncio.run(
        ensure_record(
            endpoint,
            {"slug": "proxmox-node"},
            {
                "name": "Proxmox Node",
                "slug": "proxmox-node",
                "color": "00bcd4",
            },
        )
    )

    assert existing == {"id": 44, "name": "Proxmox Node", "slug": "proxmox-node"}
    assert endpoint.get_calls == [{"slug": "proxmox-node"}, {"name": "Proxmox Node"}]


def test_ensure_record_reuses_unique_constraint_duplicate():
    class UniqueConstraintEndpoint(AsyncEndpoint):
        async def get(self, **kwargs):
            self.get_calls.append(kwargs)
            if kwargs.get("name") == "pve01" and kwargs.get("site_id") == 22:
                return {"id": 88, "name": "pve01", "site": 22}
            return None

        async def create(self, payload):
            self.created_payload = payload
            raise RuntimeError('{"__all__":["Device name must be unique per site."]}')

    endpoint = UniqueConstraintEndpoint()

    existing = asyncio.run(
        ensure_record(
            endpoint,
            {"name": "pve01"},
            {
                "name": "pve01",
                "site": 22,
                "status": "active",
            },
        )
    )

    assert existing == {"id": 88, "name": "pve01", "site": 22}
    assert endpoint.get_calls == [{"name": "pve01"}, {"name": "pve01", "site_id": 22}]


def test_ensure_tag_creates_missing_tag():
    facade = AsyncNetBoxFacade()
    created = asyncio.run(
        ensure_tag(
            facade,
            name="Proxbox",
            slug="proxbox",
            color="9e9e9e",
            description="Synced by proxbox-api",
        )
    )
    assert created == {"id": 99}
    assert facade.extras.tags.created_payload["slug"] == "proxbox"


def test_proxbox_tag_wraps_tag_creation_failures():
    with pytest.raises(ProxboxException, match="Error ensuring Proxbox tag"):
        asyncio.run(proxbox_tag(AsyncFailingTagFacade()))


def test_ensure_tag_async_uses_rest_client():
    session = AsyncNetBoxRestFacade(
        {
            ("GET", "/api/extras/tags/"): (200, {"count": 0, "results": []}),
            ("POST", "/api/extras/tags/"): (201, {"id": 99, "name": "Proxbox", "slug": "proxbox"}),
        }
    )

    created = asyncio.run(
        ensure_tag_async(
            session,
            name="Proxbox",
            slug="proxbox",
            color="9e9e9e",
            description="Synced by proxbox-api",
        )
    )

    assert created.id == 99
    assert session.client.calls == [
        ("GET", "/api/extras/tags/", {"slug": "proxbox", "limit": 2}, None, True),
        (
            "POST",
            "/api/extras/tags/",
            None,
            {
                "name": "Proxbox",
                "slug": "proxbox",
                "color": "9e9e9e",
                "description": "Synced by proxbox-api",
            },
            True,
        ),
    ]


def test_rest_create_returns_tag_record_that_can_save_and_delete():
    api = AsyncNetBoxRestFacade(
        {
            ("POST", "/api/extras/tags/"): (
                201,
                {
                    "id": 101,
                    "name": "proxbox-test",
                    "slug": "proxbox-test",
                    "color": "9e9e9e",
                    "url": "https://netbox.local/api/extras/tags/101/",
                },
            ),
            ("PATCH", "/api/extras/tags/101/"): (
                200,
                {
                    "id": 101,
                    "name": "proxbox-test",
                    "slug": "proxbox-test",
                    "color": "ffffff",
                    "url": "https://netbox.local/api/extras/tags/101/",
                },
            ),
            ("DELETE", "/api/extras/tags/101/"): (204, ""),
        }
    )

    record = rest_create(
        SyncProxy(api),
        "/api/extras/tags/",
        {
            "name": "proxbox-test",
            "slug": "proxbox-test",
            "color": "9e9e9e",
        },
    )

    record.color = "ffffff"
    assert record.save().color == "ffffff"
    assert record.delete() is True


def test_rest_ensure_async_reuses_duplicate_resource_via_payload_fallback():
    session = AsyncNetBoxRestFacade(
        {
            ("GET", "/api/virtualization/cluster-types/"): lambda query, payload: (
                200,
                {
                    "count": 1 if query.get("name") == "Cluster" else 0,
                    "results": (
                        [{"id": 11, "name": "Cluster", "slug": "cluster"}]
                        if query.get("name") == "Cluster"
                        else []
                    ),
                },
            ),
            ("POST", "/api/virtualization/cluster-types/"): (
                400,
                {
                    "name": ["cluster type with this name already exists."],
                    "slug": ["cluster type with this slug already exists."],
                },
            ),
        }
    )

    reused = asyncio.run(
        rest_ensure_async(
            session,
            "/api/virtualization/cluster-types/",
            lookup={"slug": "cluster"},
            payload={
                "name": "Cluster",
                "slug": "cluster",
                "description": "Proxmox cluster mode",
            },
        )
    )

    assert reused.id == 11
    assert session.client.calls == [
        ("GET", "/api/virtualization/cluster-types/", {"slug": "cluster", "limit": 2}, None, True),
        (
            "GET",
            "/api/virtualization/cluster-types/",
            {"name": "Cluster", "limit": 2},
            None,
            True,
        ),
    ]


def test_rest_ensure_async_reuses_unique_constraint_duplicate():
    session = AsyncNetBoxRestFacade(
        {
            ("GET", "/api/dcim/devices/"): lambda query, payload: (
                200,
                {
                    "count": 1
                    if query.get("name") == "pve01" and query.get("site_id") == 22
                    else 0,
                    "results": (
                        [{"id": 77, "name": "pve01", "site": 22}]
                        if query.get("name") == "pve01" and query.get("site_id") == 22
                        else []
                    ),
                },
            ),
            ("POST", "/api/dcim/devices/"): (
                400,
                {"__all__": ["Device name must be unique per site."]},
            ),
        }
    )

    reused = asyncio.run(
        rest_ensure_async(
            session,
            "/api/dcim/devices/",
            lookup={"name": "pve01"},
            payload={
                "name": "pve01",
                "site": 22,
                "status": "active",
            },
        )
    )

    assert reused.id == 77
    assert session.client.calls == [
        ("GET", "/api/dcim/devices/", {"name": "pve01", "limit": 2}, None, True),
        ("GET", "/api/dcim/devices/", {"name": "pve01", "site_id": 22, "limit": 2}, None, True),
    ]


def test_rest_reconcile_async_patches_only_schema_detected_changes():
    session = AsyncNetBoxRestFacade(
        {
            ("GET", "/api/dcim/devices/"): (
                200,
                {
                    "count": 1,
                    "results": [
                        {
                            "id": 55,
                            "name": "pve01",
                            "status": {"value": "active"},
                            "cluster": {"id": 12, "name": "lab"},
                            "device_type": {"id": 14, "model": "Proxmox Generic Device"},
                            "role": {"id": 15, "name": "Proxmox Node"},
                            "site": {"id": 16, "name": "Proxmox Default Site - lab"},
                            "description": "old description",
                            "tags": [{"slug": "proxbox"}],
                            "url": "https://netbox.local/api/dcim/devices/55/",
                        }
                    ],
                },
            ),
            ("PATCH", "/api/dcim/devices/55/"): (
                200,
                {
                    "id": 55,
                    "name": "pve01",
                    "status": {"value": "active"},
                    "cluster": {"id": 12, "name": "lab"},
                    "device_type": {"id": 14, "model": "Proxmox Generic Device"},
                    "role": {"id": 15, "name": "Proxmox Node"},
                    "site": {"id": 16, "name": "Proxmox Default Site - lab"},
                    "description": "Proxmox Node pve01",
                    "tags": [{"slug": "proxbox"}],
                    "url": "https://netbox.local/api/dcim/devices/55/",
                },
            ),
        }
    )

    updated = asyncio.run(
        rest_reconcile_async(
            session,
            "/api/dcim/devices/",
            lookup={"name": "pve01", "site_id": 16},
            payload={
                "name": "pve01",
                "status": "active",
                "cluster": 12,
                "device_type": 14,
                "role": 15,
                "site": 16,
                "description": "Proxmox Node pve01",
                "tags": ["proxbox"],
            },
            schema=NetBoxDeviceSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "status": record.get("status"),
                "cluster": record.get("cluster"),
                "device_type": record.get("device_type"),
                "role": record.get("role"),
                "site": record.get("site"),
                "description": record.get("description"),
                "tags": record.get("tags"),
            },
        )
    )

    assert updated.description == "Proxmox Node pve01"
    assert session.client.calls == [
        ("GET", "/api/dcim/devices/", {"name": "pve01", "site_id": 16, "limit": 2}, None, True),
        (
            "PATCH",
            "/api/dcim/devices/55/",
            None,
            {"description": "Proxmox Node pve01"},
            True,
        ),
    ]


def test_rest_reconcile_async_reuses_duplicate_site_after_failed_create():
    site_queries = {"count": 0}

    session = AsyncNetBoxRestFacade(
        {
            ("GET", "/api/dcim/sites/"): lambda query, payload: (
                200,
                {
                    "count": (
                        1
                        if query.get("name") == "Proxmox Default Site - lab"
                        and site_queries.__setitem__("count", site_queries["count"] + 1) is None
                        and site_queries["count"] > 1
                        else 0
                    ),
                    "results": (
                        [
                            {
                                "id": 16,
                                "name": "Proxmox Default Site - lab",
                                "slug": "proxmox-default-site-lab",
                                "status": {"value": "active"},
                                "tags": [{"slug": "proxbox", "name": "Proxbox"}],
                                "url": "https://netbox.local/api/dcim/sites/16/",
                            }
                        ]
                        if query.get("name") == "Proxmox Default Site - lab"
                        and site_queries["count"] > 1
                        else []
                    ),
                },
            ),
            ("POST", "/api/dcim/sites/"): (
                400,
                {
                    "name": ["site with this name already exists."],
                    "slug": ["site with this slug already exists."],
                },
            ),
        }
    )

    reused = asyncio.run(
        rest_reconcile_async(
            session,
            "/api/dcim/sites/",
            lookup={"slug": "proxmox-default-site-lab"},
            payload={
                "name": "Proxmox Default Site - lab",
                "slug": "proxmox-default-site-lab",
                "status": "active",
                "tags": [{"slug": "proxbox", "name": "Proxbox"}],
            },
            schema=NetBoxSiteSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "status": record.get("status"),
                "tags": record.get("tags"),
            },
        )
    )

    assert reused.id == 16
    assert session.client.calls == [
        ("GET", "/api/dcim/sites/", {"slug": "proxmox-default-site-lab", "limit": 2}, None, True),
        ("GET", "/api/dcim/sites/", {"name": "Proxmox Default Site - lab", "limit": 2}, None, True),
        (
            "POST",
            "/api/dcim/sites/",
            None,
            {
                "name": "Proxmox Default Site - lab",
                "slug": "proxmox-default-site-lab",
                "status": "active",
                "tags": [{"slug": "proxbox", "name": "Proxbox"}],
                "custom_fields": {},
            },
            True,
        ),
        ("GET", "/api/dcim/sites/", {"slug": "proxmox-default-site-lab", "limit": 2}, None, True),
        ("GET", "/api/dcim/sites/", {"name": "Proxmox Default Site - lab", "limit": 2}, None, True),
    ]


def test_sync_proxy_runs_async_methods_synchronously():
    facade = SyncProxy(AsyncNetBoxFacade())
    assert facade.status() == {"netbox": "ok"}


def test_sync_proxy_does_not_treat_plain_namespace_as_async_iterable():
    proxy = SyncProxy(SimpleNamespace(app=FauxAsyncNamespace()))

    app = proxy.app

    assert isinstance(app, SyncProxy)
    assert app.child.value == 1


def test_get_netbox_session_wraps_async_facade(monkeypatch, db_engine):
    with Session(db_engine) as session:
        session.add(
            NetBoxEndpoint(
                name="netbox",
                ip_address="10.0.0.20",
                domain="netbox.local",
                port=443,
                token="secret",
                verify_ssl=True,
            )
        )
        session.commit()

        monkeypatch.setattr(
            netbox_session_module,
            "netbox_api_from_endpoint",
            lambda ep: AsyncNetBoxFacade(),
        )
        wrapped = netbox_session_module.get_netbox_session(session)

    assert wrapped.status() == {"netbox": "ok"}


def test_get_netbox_session_requires_endpoint(db_engine):
    with Session(db_engine) as session:
        with pytest.raises(ProxboxException) as excinfo:
            netbox_session_module.get_netbox_session(session)
        assert excinfo.value.message in [
            "No NetBox endpoint found",
            "Error establishing NetBox API session",
        ]


def test_get_netbox_async_session_returns_async_facade(monkeypatch, db_engine):
    with Session(db_engine) as session:
        session.add(
            NetBoxEndpoint(
                name="netbox",
                ip_address="10.0.0.20",
                domain="netbox.local",
                port=443,
                token="secret",
                verify_ssl=True,
            )
        )
        session.commit()

        async_facade = AsyncNetBoxFacade()
        monkeypatch.setattr(
            netbox_session_module,
            "netbox_api_from_endpoint",
            lambda ep: async_facade,
        )
        returned = get_netbox_async_session(session)

    assert returned is async_facade


def test_typed_cluster_status_wraps_model_validation_failures(monkeypatch):
    session = FakeTypedProxmoxSession()

    monkeypatch.setattr(
        "proxbox_api.services.proxmox_helpers.generated_models.GetClusterStatusResponse.model_validate",
        lambda payload: (_ for _ in ()).throw(ValueError("bad cluster payload")),
    )

    with pytest.raises(ProxboxException, match="Error fetching Proxmox cluster status"):
        get_typed_cluster_status(session)


def test_typed_vm_config_wraps_model_validation_failures(monkeypatch):
    session = FakeTypedProxmoxSession()

    monkeypatch.setattr(
        "proxbox_api.services.proxmox_helpers.generated_models.GetNodesNodeQemuVmidConfigResponse.model_validate",
        lambda payload: (_ for _ in ()).throw(ValueError("bad vm config")),
    )

    with pytest.raises(ProxboxException, match="Error fetching Proxmox VM config"):
        get_typed_vm_config(session, node="pve01", vm_type="qemu", vmid=101)


def test_proxmox_session_supports_token_auth(monkeypatch):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", FakeProxmoxAPI)

    session = ProxmoxSession(
        {
            "ip_address": "10.0.0.10",
            "domain": "pve.local",
            "http_port": 8006,
            "user": "root@pam",
            "password": None,
            "token": {"name": "sync", "value": "secret"},
            "ssl": False,
        }
    )

    assert session.CONNECTED is True
    assert session.mode == "cluster"
    assert session.name == "lab-cluster"
    assert session.fingerprints == ["fingerprint"]


def test_proxmox_session_falls_back_to_ip_when_domain_fails(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.session.proxmox.ProxmoxAPI",
        FakeFailThenSucceedProxmoxAPI,
    )

    session = ProxmoxSession(
        {
            "ip_address": "10.0.0.10",
            "domain": "pve.local",
            "http_port": 8006,
            "user": "root@pam",
            "password": "password",
            "token": {"name": None, "value": None},
            "ssl": False,
        }
    )

    assert session.CONNECTED is True
    assert getattr(session.proxmoxer, "host", None) == "10.0.0.10"


def test_proxmox_session_normalizes_token_string_value(monkeypatch):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", FakeProxmoxAPI)

    session = ProxmoxSession(
        {
            "ip_address": "10.0.0.10",
            "domain": "pve.local",
            "http_port": 8006,
            "user": "root@pam",
            "password": None,
            "token": {
                "name": "",
                "value": "PVEAPIToken=root@pam!sync=secret-value",
            },
            "ssl": False,
        }
    )

    assert session.CONNECTED is True
    assert session.token_name == "sync"
    assert session.token_value == "secret-value"
    assert session.proxmoxer.kwargs["token_name"] == "sync"
    assert session.proxmoxer.kwargs["token_value"] == "secret-value"


def test_proxmox_session_allows_version_when_cluster_status_permission_denied(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.session.proxmox.ProxmoxAPI",
        FakePermissionDeniedProxmoxAPI,
    )

    session = ProxmoxSession(
        {
            "ip_address": "10.0.0.10",
            "domain": None,
            "http_port": 8006,
            "user": "root@pam",
            "password": None,
            "token": {"name": "proxbox2", "value": "secret"},
            "ssl": False,
        }
    )

    assert session.CONNECTED is True
    assert session.permission_limited is True
    assert session.mode == "restricted"
    assert session.name == "10.0.0.10"
    assert session.version == {"version": "8.3.0"}


def test_proxmox_sessions_reads_database_endpoints(monkeypatch, db_engine):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", FakeProxmoxAPI)

    with Session(db_engine) as session:
        session.add(
            ProxmoxEndpoint(
                name="pve01",
                ip_address="10.0.0.10",
                domain="pve.local",
                port=8006,
                username="root@pam",
                password="password",
                verify_ssl=False,
            )
        )
        session.commit()
        sessions = asyncio.run(proxmox_sessions(session))

    assert len(sessions) == 1
    assert sessions[0].name == "lab-cluster"


def test_proxmox_sessions_reads_netbox_endpoints_async(monkeypatch, db_engine):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", FakeProxmoxAPI)

    monkeypatch.setattr(
        "proxbox_api.session.proxmox_providers.get_netbox_async_session",
        lambda database_session: AsyncNetBoxRestFacade(
            {
                ("GET", "/api/plugins/proxbox/endpoints/proxmox/"): (
                    200,
                    {
                        "count": 1,
                        "results": [
                            {
                                "ip_address": {"address": "10.0.0.10/24"},
                                "domain": "pve.local",
                                "port": 8006,
                                "username": "root@pam",
                                "password": "password",
                                "verify_ssl": False,
                                "token_name": None,
                                "token_value": None,
                            }
                        ],
                    },
                )
            }
        ),
    )

    with Session(db_engine) as session:
        sessions = asyncio.run(proxmox_sessions(session, source="netbox"))

    assert len(sessions) == 1
    assert sessions[0].name == "lab-cluster"


def test_typed_proxmox_helpers_validate_live_payloads():
    session = FakeTypedProxmoxSession()

    cluster_items = get_typed_cluster_status(session)
    resource_items = get_typed_cluster_resources(session)
    storage_items = get_typed_storage_list(session)
    backup_items = get_typed_node_storage_content(
        session, node="pve01", storage="local", vmid="101", content="backup"
    )
    vm_config = get_typed_vm_config(session, node="pve01", vm_type="qemu", vmid=101)

    assert cluster_items[0].type == "cluster"
    assert cluster_items[1].name == "pve01"
    assert resource_items[0].vmid == 101
    assert storage_items[0].storage == "local"
    assert backup_items[0].volid == "local:backup/vzdump-qemu-101.vma.zst"
    assert vm_config.name == "vm01"
    assert vm_config.digest == "abc123"


def test_proxmox_routes_use_typed_helpers_for_sync_dependencies():
    pxs = [FakeTypedProxmoxSession()]

    cluster_status_payload = asyncio.run(cluster_status(pxs))
    cluster_resources_payload = asyncio.run(cluster_resources(pxs, type=None))
    vm_config_payload = asyncio.run(
        get_vm_config(pxs, cluster_status_payload, node="pve01", type="qemu", vmid=101)
    )
    backup_payload = asyncio.run(
        get_proxmox_node_storage_content(
            pxs,
            cluster_status_payload,
            node="pve01",
            storage="local",
            vmid="101",
            content="backup",
        )
    )

    assert cluster_status_payload[0].name == "lab"
    assert cluster_status_payload[0].node_list[0].name == "pve01"
    assert cluster_resources_payload == [
        {
            "lab": [
                {
                    "id": "qemu/101",
                    "name": "vm01",
                    "node": "pve01",
                    "status": "running",
                    "type": "qemu",
                    "vmid": 101,
                }
            ]
        }
    ]
    assert vm_config_payload["name"] == "vm01"
    assert vm_config_payload["digest"] == "abc123"
    assert vm_config_payload["memory"] == "4096"
    assert vm_config_payload["net0"].startswith("virtio=")
    assert backup_payload[0]["volid"] == "local:backup/vzdump-qemu-101.vma.zst"
    assert backup_payload[0]["content"] == "backup"


def test_netbox_v2_config_produces_bearer_authorization():
    from netbox_sdk.config import authorization_header_value

    from proxbox_api.session.netbox import netbox_config_from_endpoint

    ep = NetBoxEndpoint(
        name="nb",
        ip_address="10.0.0.2",
        domain="nb.example.com",
        port=443,
        token_version="v2",
        token_key="myid",
        token="s3cr37",
        verify_ssl=True,
    )
    auth = authorization_header_value(netbox_config_from_endpoint(ep))
    assert auth == "Bearer nbt_myid.s3cr37"


def test_netbox_v1_config_produces_token_authorization():
    from netbox_sdk.config import authorization_header_value

    from proxbox_api.session.netbox import netbox_config_from_endpoint

    ep = NetBoxEndpoint(
        name="nb",
        ip_address="10.0.0.2",
        domain="nb.example.com",
        port=443,
        token_version="v1",
        token_key=None,
        token="abc123deadbeef",
        verify_ssl=True,
    )
    auth = authorization_header_value(netbox_config_from_endpoint(ep))
    assert auth == "Token abc123deadbeef"


def test_ensure_device_preserves_existing_site_different_from_sync_default():
    """Test that _ensure_device preserves an existing device's user-assigned site.

    When a device already exists at a site different from the sync default site,
    subsequent syncs must NOT patch the site back to the sync default.
    Regression test for https://github.com/emersonfelipesp/netbox-proxbox/issues/145
    """
    sync_default_site_id = 16
    user_assigned_site_id = 99

    patch_calls = []

    class SitePreservingRestClient(RestClientStub):
        async def request(self, method, path, *, query=None, payload=None, expect_json=True):
            if method == "PATCH":
                patch_calls.append((path, payload))
            return await super().request(
                method, path, query=query, payload=payload, expect_json=expect_json
            )

    def make_get_response(query, payload):
        if query.get("name") == "pve01" and query.get("limit") == 2:
            return (
                200,
                {
                    "count": 1,
                    "results": [
                        {
                            "id": 50,
                            "name": "pve01",
                            "site": user_assigned_site_id,
                            "status": "active",
                            "cluster": 12,
                            "device_type": 14,
                            "role": 15,
                            "description": "Proxmox Node pve01",
                            "tags": [],
                            "custom_fields": {},
                        }
                    ],
                },
            )
        if query.get("name") == "pve01" and query.get("site_id") == sync_default_site_id:
            return (200, {"count": 0, "results": []})
        return (200, {"count": 0, "results": []})

    responses = {
        ("GET", "/api/dcim/devices/"): make_get_response,
        ("PATCH", "/api/dcim/devices/50/"): (
            200,
            {"id": 50, "name": "pve01", "site": user_assigned_site_id},
        ),
        ("POST", "/api/dcim/sites/"): (201, {"id": 16, "name": "Proxmox Default Site - lab"}),
        ("POST", "/api/dcim/manufacturers/"): (201, {"id": 13, "name": "Proxmox"}),
        ("POST", "/api/dcim/device-types/"): (201, {"id": 14, "model": "Proxmox Generic Device"}),
        ("POST", "/api/dcim/device-roles/"): (201, {"id": 15, "name": "Proxmox Node"}),
        ("POST", "/api/virtualization/cluster-types/"): (201, {"id": 11, "name": "Cluster"}),
        ("POST", "/api/virtualization/clusters/"): (201, {"id": 12, "name": "lab"}),
        ("GET", "/api/dcim/sites/"): (200, {"count": 0, "results": []}),
        ("GET", "/api/dcim/manufacturers/"): (200, {"count": 0, "results": []}),
        ("GET", "/api/dcim/device-types/"): (200, {"count": 0, "results": []}),
        ("GET", "/api/dcim/device-roles/"): (200, {"count": 0, "results": []}),
        ("GET", "/api/virtualization/cluster-types/"): (200, {"count": 0, "results": []}),
        ("GET", "/api/virtualization/clusters/"): (200, {"count": 0, "results": []}),
    }

    facade = AsyncNetBoxRestFacade(responses)
    facade.client = SitePreservingRestClient(responses)

    from proxbox_api.services.sync.device_ensure import _ensure_device

    asyncio.run(
        _ensure_device(
            nb=facade,
            device_name="pve01",
            cluster_id=12,
            device_type_id=14,
            role_id=15,
            site_id=sync_default_site_id,
            tag_refs=[],
        )
    )

    for path, payload in patch_calls:
        if payload is not None:
            assert payload.get("site") != sync_default_site_id, (
                f"PATCH to {path} should NOT contain sync default site_id={sync_default_site_id}. "
                f"Got payload: {payload}"
            )
