"""Tests for proxbox-api firewall write routes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from proxmox_sdk.sdk.exceptions import ResourceException
from sqlmodel import Session

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.main import app

WRITE_ROUTE_MATRIX = {
    "/proxmox/firewall/datacenter/rules": {"post"},
    "/proxmox/firewall/datacenter/rules/{pos}": {"put", "delete"},
    "/proxmox/firewall/datacenter/groups": {"post"},
    "/proxmox/firewall/datacenter/groups/{group}": {"delete"},
    "/proxmox/firewall/datacenter/groups/{group}/rules": {"post"},
    "/proxmox/firewall/datacenter/groups/{group}/rules/{pos}": {"put", "delete"},
    "/proxmox/firewall/datacenter/ipsets": {"post"},
    "/proxmox/firewall/datacenter/ipsets/{name}": {"delete"},
    "/proxmox/firewall/datacenter/ipsets/{name}/entries": {"post"},
    "/proxmox/firewall/datacenter/ipsets/{name}/entries/{cidr}": {"put", "delete"},
    "/proxmox/firewall/datacenter/aliases": {"post"},
    "/proxmox/firewall/datacenter/aliases/{name}": {"put", "delete"},
    "/proxmox/firewall/datacenter/options": {"put"},
    "/proxmox/firewall/nodes/{node}/rules": {"post"},
    "/proxmox/firewall/nodes/{node}/rules/{pos}": {"put", "delete"},
    "/proxmox/firewall/nodes/{node}/options": {"put"},
    "/proxmox/firewall/vms/{vmid}/rules": {"post"},
    "/proxmox/firewall/vms/{vmid}/rules/{pos}": {"put", "delete"},
    "/proxmox/firewall/vms/{vmid}/ipsets": {"post"},
    "/proxmox/firewall/vms/{vmid}/ipsets/{name}": {"delete"},
    "/proxmox/firewall/vms/{vmid}/ipsets/{name}/entries": {"post"},
    "/proxmox/firewall/vms/{vmid}/ipsets/{name}/entries/{cidr}": {"put", "delete"},
    "/proxmox/firewall/vms/{vmid}/aliases": {"post"},
    "/proxmox/firewall/vms/{vmid}/aliases/{name}": {"put", "delete"},
    "/proxmox/firewall/vms/{vmid}/options": {"put"},
    "/proxmox/firewall/vnets/{vnet}/rules": {"post"},
    "/proxmox/firewall/vnets/{vnet}/rules/{pos}": {"put", "delete"},
}


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


def test_openapi_contains_full_firewall_write_matrix():
    paths = app.openapi()["paths"]
    for route, methods in WRITE_ROUTE_MATRIX.items():
        assert route in paths
        available = {method.lower() for method in paths[route]}
        assert methods.issubset(available), (route, methods, available)


@pytest.mark.parametrize(
    ("method", "url", "json_body", "expected_call"),
    [
        (
            "put",
            "/proxmox/firewall/datacenter/aliases/web",
            {"cidr": "10.0.0.10", "comment": "web"},
            _Call("put", "cluster/firewall/aliases/web", {"cidr": "10.0.0.10", "comment": "web"}),
        ),
        (
            "post",
            "/proxmox/firewall/datacenter/ipsets/mgmt/entries",
            {"cidr": "10.0.0.0/8", "nomatch": False},
            _Call("post", "cluster/firewall/ipset/mgmt", {"cidr": "10.0.0.0/8", "nomatch": False}),
        ),
        (
            "put",
            "/proxmox/firewall/nodes/pve-a/options",
            {"enable": True, "policy_in": "DROP"},
            _Call("put", "nodes/pve-a/firewall/options", {"enable": True, "policy_in": "DROP"}),
        ),
        (
            "post",
            "/proxmox/firewall/vms/101/rules?node=pve-a&vm_type=lxc",
            {"type": "in", "action": "ACCEPT", "enable": True},
            _Call(
                "post",
                "nodes/pve-a/lxc/101/firewall/rules",
                {"type": "in", "action": "ACCEPT", "enable": True},
            ),
        ),
        (
            "put",
            "/proxmox/firewall/vms/101/options?node=pve-a&vm_type=qemu",
            {"enable": True, "policy_out": "ACCEPT"},
            _Call(
                "put",
                "nodes/pve-a/qemu/101/firewall/options",
                {"enable": True, "policy_out": "ACCEPT"},
            ),
        ),
    ],
)
def test_representative_write_routes_dispatch_to_expected_sdk_paths(
    auth_test_client,
    db_engine,
    monkeypatch,
    method,
    url,
    json_body,
    expected_call,
):
    endpoint_id = _make_endpoint(db_engine, allow_writes=True)
    fake = _FakeProxmoxSession()

    import proxbox_api.routes.proxmox.firewall as firewall_routes

    async def _open(_endpoint):
        return fake

    monkeypatch.setattr(firewall_routes, "_open_proxmox_session", _open)

    separator = "&" if "?" in url else "?"
    response = getattr(auth_test_client, method)(
        f"{url}{separator}endpoint_id={endpoint_id}",
        headers={"X-Proxbox-Actor": "ops@example.com"},
        json=json_body,
    )

    assert response.status_code == 200, response.text
    assert fake.calls == [expected_call]
