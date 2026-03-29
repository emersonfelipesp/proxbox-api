from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from proxbox_api.database import NetBoxEndpoint, ProxmoxEndpoint
from proxbox_api.dependencies import proxbox_tag
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_sdk_helpers import ensure_record, ensure_tag, to_dict
from proxbox_api.netbox_sdk_sync import SyncProxy
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
from proxbox_api.session.proxmox import ProxmoxSession, proxmox_sessions
from proxbox_api.session.netbox import get_netbox_async_session


class AsyncEndpoint:
    def __init__(self, *, existing=None, created=None):
        self.existing = existing
        self.created = created or {"id": 99}
        self.created_payload = None

    async def get(self, **kwargs):
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


class AsyncNetBoxPluginsFacade:
    def __init__(self, items):
        self.plugins = SimpleNamespace(
            proxbox=SimpleNamespace(
                __getattr__=lambda name: (
                    AsyncIterableEndpoint(items) if name == "endpoints/proxmox" else None
                )
            )
        )


class BrokenAsyncEndpoint:
    def all(self):
        raise ValueError("Resource does not expose list path: plugins/proxbox/endpoints/proxmox")


class AsyncNetBoxFallbackFacade:
    def __init__(self, payload):
        self._payload = payload
        self.plugins = SimpleNamespace(
            proxbox=SimpleNamespace(
                __getattr__=lambda name: (
                    BrokenAsyncEndpoint() if name == "endpoints/proxmox" else None
                )
            )
        )
        self.client = SimpleNamespace(request=self._request)

    async def _request(self, method, path):
        assert method == "GET"
        assert path == "/api/plugins/proxbox/endpoints/proxmox/"
        return SimpleNamespace(json=lambda: self._payload)


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


def test_sync_proxy_runs_async_methods_synchronously():
    facade = SyncProxy(AsyncNetBoxFacade())
    assert facade.status() == {"netbox": "ok"}


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
        with pytest.raises(ProxboxException, match="No NetBox endpoint found"):
            netbox_session_module.get_netbox_session(session)


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

    endpoint = SimpleNamespace(
        ip_address=SimpleNamespace(address="10.0.0.10/24"),
        domain="pve.local",
        port=8006,
        username="root@pam",
        password="password",
        verify_ssl=False,
        token_name=None,
        token_value=None,
    )

    monkeypatch.setattr(
        "proxbox_api.session.proxmox.get_netbox_async_session",
        lambda database_session: AsyncNetBoxPluginsFacade([endpoint]),
    )

    with Session(db_engine) as session:
        sessions = asyncio.run(proxmox_sessions(session, source="netbox"))

    assert len(sessions) == 1
    assert sessions[0].name == "lab-cluster"


def test_proxmox_sessions_reads_netbox_endpoints_via_client_fallback(monkeypatch, db_engine):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", FakeProxmoxAPI)

    payload = {
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
    }

    monkeypatch.setattr(
        "proxbox_api.session.proxmox.get_netbox_async_session",
        lambda database_session: AsyncNetBoxFallbackFacade(payload),
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
