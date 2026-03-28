from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from proxbox_api.database import NetBoxEndpoint, ProxmoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_sdk_helpers import ensure_record, ensure_tag, to_dict
from proxbox_api.netbox_sdk_sync import SyncProxy
from proxbox_api.session import netbox as netbox_session_module
from proxbox_api.session.proxmox import ProxmoxSession, proxmox_sessions


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


class AsyncNetBoxFacade:
    def __init__(self):
        self.status_calls = 0
        self.extras = SimpleNamespace(tags=AsyncTagsEndpoint())

    async def status(self):
        self.status_calls += 1
        return {"netbox": "ok"}


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


@dataclass
class SerializableRecord:
    id: int

    def serialize(self):
        return {"id": self.id}


def test_to_dict_supports_dict_and_serializable_objects():
    assert to_dict({"id": 1}) == {"id": 1}
    assert to_dict(SerializableRecord(id=2)) == {"id": 2}
    assert to_dict(object()) == {}


@pytest.mark.asyncio
async def test_ensure_record_get_or_create_behavior():
    existing_endpoint = AsyncEndpoint(existing={"id": 10})
    created_endpoint = AsyncEndpoint(existing=None, created={"id": 11})

    existing = await ensure_record(existing_endpoint, {"name": "vm01"}, {"name": "vm01"})
    created = await ensure_record(created_endpoint, {"name": "vm02"}, {"name": "vm02"})

    assert existing == {"id": 10}
    assert created == {"id": 11}
    assert created_endpoint.created_payload == {"name": "vm02"}


@pytest.mark.asyncio
async def test_ensure_tag_creates_missing_tag():
    facade = AsyncNetBoxFacade()
    created = await ensure_tag(
        facade,
        name="Proxbox",
        slug="proxbox",
        color="9e9e9e",
        description="Synced by proxbox-api",
    )
    assert created == {"id": 99}
    assert facade.extras.tags.created_payload["slug"] == "proxbox"


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
            "api",
            lambda url, token: AsyncNetBoxFacade(),
        )
        wrapped = netbox_session_module.get_netbox_session(session)

    assert wrapped.status() == {"netbox": "ok"}


def test_get_netbox_session_requires_endpoint(db_engine):
    with Session(db_engine) as session:
        with pytest.raises(ProxboxException, match="No NetBox endpoint found"):
            netbox_session_module.get_netbox_session(session)


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
    assert session.proxmoxer.host == "10.0.0.10"


@pytest.mark.asyncio
async def test_proxmox_sessions_reads_database_endpoints(monkeypatch, db_engine):
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
        sessions = await proxmox_sessions(session)

    assert len(sessions) == 1
    assert sessions[0].name == "lab-cluster"
