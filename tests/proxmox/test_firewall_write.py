"""Tests for proxbox-api firewall write routes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from proxmox_sdk.sdk.exceptions import ResourceException
from sqlmodel import Session

from proxbox_api.database import ProxmoxEndpoint


def _make_endpoint(db_engine, *, allow_writes: bool) -> int:
    with Session(db_engine) as session:
        endpoint = ProxmoxEndpoint(
            name="pve-test",
            ip_address="10.0.0.10",
            port=8006,
            username="root@pam",
            verify_ssl=False,
            allow_writes=allow_writes,
        )
        session.add(endpoint)
        session.commit()
        session.refresh(endpoint)
        assert endpoint.id is not None
        return endpoint.id


def _make_501() -> ResourceException:
    return ResourceException(
        status_code=501,
        status_message="Not Implemented",
        content="Method not implemented",
    )


@dataclass
class _Call:
    method: str
    path: str
    payload: dict[str, Any]


@dataclass
class _FakeProxmoxSession:
    calls: list[_Call] = field(default_factory=list)
    exc: Exception | None = None

    def session(self, path: str):
        calls = self.calls
        exc = self.exc

        class _Resource:
            async def get(self):
                if exc is not None:
                    raise exc
                calls.append(_Call("get", path, {}))
                return []

            async def post(self, **payload):
                if exc is not None:
                    raise exc
                calls.append(_Call("post", path, payload))
                return {"data": "UPID:pve-test:1"}

            async def put(self, **payload):
                if exc is not None:
                    raise exc
                calls.append(_Call("put", path, payload))
                return {"ok": True}

            async def delete(self, **payload):
                if exc is not None:
                    raise exc
                calls.append(_Call("delete", path, payload))
                return None

        return _Resource()

    async def aclose(self) -> None:
        return None


def test_firewall_write_disabled_returns_403(auth_test_client, db_engine):
    endpoint_id = _make_endpoint(db_engine, allow_writes=False)

    response = auth_test_client.post(
        "/proxmox/firewall/datacenter/rules",
        params={"endpoint_id": endpoint_id},
        headers={"X-Proxbox-Actor": "ops@example.com"},
        json={"type": "in", "action": "ACCEPT", "enable": True},
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "writes_disabled_for_endpoint"


def test_firewall_write_requires_actor(auth_test_client, db_engine):
    endpoint_id = _make_endpoint(db_engine, allow_writes=True)

    response = auth_test_client.post(
        "/proxmox/firewall/datacenter/rules",
        params={"endpoint_id": endpoint_id},
        json={"type": "in", "action": "ACCEPT", "enable": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "actor_required"


def test_datacenter_rule_create_dispatches_to_proxmox_sdk(
    auth_test_client,
    db_engine,
    monkeypatch,
):
    endpoint_id = _make_endpoint(db_engine, allow_writes=True)
    fake = _FakeProxmoxSession()

    import proxbox_api.routes.proxmox.firewall as firewall_routes

    async def _open(_endpoint):
        return fake

    monkeypatch.setattr(firewall_routes, "_open_proxmox_session", _open)

    response = auth_test_client.post(
        "/proxmox/firewall/datacenter/rules",
        params={"endpoint_id": endpoint_id},
        headers={"X-Proxbox-Actor": "ops@example.com"},
        json={"type": "in", "action": "ACCEPT", "enable": True, "comment": "allow"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pushed"
    assert body["proxmox_task_id"] == "UPID:pve-test:1"
    assert fake.calls == [
        _Call(
            "post",
            "cluster/firewall/rules",
            {"type": "in", "action": "ACCEPT", "enable": True, "comment": "allow"},
        )
    ]


def test_vnet_501_returns_skipped(auth_test_client, db_engine, monkeypatch):
    endpoint_id = _make_endpoint(db_engine, allow_writes=True)
    fake = _FakeProxmoxSession(exc=_make_501())

    import proxbox_api.routes.proxmox.firewall as firewall_routes

    async def _open(_endpoint):
        return fake

    monkeypatch.setattr(firewall_routes, "_open_proxmox_session", _open)

    response = auth_test_client.post(
        "/proxmox/firewall/vnets/vnet0/rules",
        params={"endpoint_id": endpoint_id},
        headers={"X-Proxbox-Actor": "ops@example.com"},
        json={"type": "forward", "action": "ACCEPT", "enable": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "vnet_firewall_not_supported"
