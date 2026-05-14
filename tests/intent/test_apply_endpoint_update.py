"""Tests for UPDATE ``POST /intent/apply``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session

from proxbox_api.database import ProxmoxEndpoint, get_async_session
from proxbox_api.main import app


class _FakeConfigEndpoint:
    def __init__(
        self,
        fake: _FakeProxmoxAPI,
        *,
        kind: str,
        node: str,
        vmid: int,
    ) -> None:
        self._fake = fake
        self._kind = kind
        self._node = node
        self._vmid = vmid

    async def get(self) -> dict[str, object]:
        return dict(self._fake.record(self._kind, self._node, self._vmid)["config"])

    async def put(self, **kwargs) -> str:
        self._fake.calls.append(
            {
                "kind": self._kind,
                "node": self._node,
                "vmid": self._vmid,
                "body": kwargs,
            }
        )
        self._fake.record(self._kind, self._node, self._vmid)["config"].update(kwargs)
        return self._fake.upid


class _FakeStatusCurrentEndpoint:
    def __init__(
        self,
        fake: _FakeProxmoxAPI,
        *,
        kind: str,
        node: str,
        vmid: int,
    ) -> None:
        self._fake = fake
        self._kind = kind
        self._node = node
        self._vmid = vmid

    async def get(self) -> dict[str, object]:
        return {"status": self._fake.record(self._kind, self._node, self._vmid)["status"]}


class _FakeStatusEndpoint:
    def __init__(
        self,
        fake: _FakeProxmoxAPI,
        *,
        kind: str,
        node: str,
        vmid: int,
    ) -> None:
        self.current = _FakeStatusCurrentEndpoint(fake, kind=kind, node=node, vmid=vmid)


class _FakeVmEndpoint:
    def __init__(
        self,
        fake: _FakeProxmoxAPI,
        *,
        kind: str,
        node: str,
        vmid: int,
    ) -> None:
        self.config = _FakeConfigEndpoint(fake, kind=kind, node=node, vmid=vmid)
        self.status = _FakeStatusEndpoint(fake, kind=kind, node=node, vmid=vmid)


class _FakeKindCollection:
    def __init__(self, fake: _FakeProxmoxAPI, *, kind: str, node: str) -> None:
        self._fake = fake
        self._kind = kind
        self._node = node

    async def get(self) -> list[dict[str, object]]:
        return [
            self._fake.resource(record)
            for record in self._fake.records.values()
            if record["kind"] == self._kind and record["node"] == self._node
        ]

    def __call__(self, vmid: int) -> _FakeVmEndpoint:
        return _FakeVmEndpoint(self._fake, kind=self._kind, node=self._node, vmid=vmid)


class _FakeNodeEndpoint:
    def __init__(self, fake: _FakeProxmoxAPI, node: str) -> None:
        self.qemu = _FakeKindCollection(fake, kind="qemu", node=node)
        self.lxc = _FakeKindCollection(fake, kind="lxc", node=node)


class _FakeNodesEndpoint:
    def __init__(self, fake: _FakeProxmoxAPI) -> None:
        self._fake = fake

    def __call__(self, node: str) -> _FakeNodeEndpoint:
        return _FakeNodeEndpoint(self._fake, node)


class _FakeClusterResourcesEndpoint:
    def __init__(self, fake: _FakeProxmoxAPI) -> None:
        self._fake = fake

    async def get(self, **kwargs) -> list[dict[str, object]]:
        del kwargs
        return [self._fake.resource(record) for record in self._fake.records.values()]


class _FakeProxmoxAPI:
    def __init__(
        self,
        records: list[dict[str, object]],
        *,
        upid: str = "UPID:pve01:0001:update",
    ) -> None:
        self.records = {
            (str(record["kind"]), str(record["node"]), int(record["vmid"])): record
            for record in records
        }
        self.calls: list[dict[str, object]] = []
        self.upid = upid
        self.nodes = _FakeNodesEndpoint(self)

    def __call__(self, path: str) -> _FakeClusterResourcesEndpoint:
        assert path == "cluster/resources"
        return _FakeClusterResourcesEndpoint(self)

    def record(self, kind: str, node: str, vmid: int) -> dict[str, object]:
        return self.records[(kind, node, vmid)]

    def resource(self, record: dict[str, object]) -> dict[str, object]:
        name = record["config"].get("name") or record["config"].get("hostname")
        return {
            "vmid": record["vmid"],
            "node": record["node"],
            "type": record["kind"],
            "name": name,
            "status": record["status"],
        }


def _record(
    *,
    kind: str = "qemu",
    node: str = "pve01",
    vmid: int = 101,
    status: str = "stopped",
    config: dict[str, object] | None = None,
) -> dict[str, object]:
    default_config = {
        "name": f"vm-{vmid}",
        "hostname": f"ct-{vmid}",
        "cores": 2,
        "memory": 1024,
        "tags": "old",
    }
    if config:
        default_config.update(config)
    return {
        "kind": kind,
        "node": node,
        "vmid": vmid,
        "status": status,
        "config": default_config,
    }


def _make_endpoint(db_session: Session, *, allow_writes: bool) -> int:
    endpoint = ProxmoxEndpoint(
        name=f"pve-apply-update-{allow_writes}",
        ip_address="10.0.0.10",
        port=8006,
        username="root@pam",
        verify_ssl=False,
        allow_writes=allow_writes,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    assert endpoint.id is not None
    endpoint_id = endpoint.id
    db_session.close()
    return endpoint_id


def _body(*diffs: dict[str, object], run_uuid: str = "run-update") -> dict[str, object]:
    return {
        "run_uuid": run_uuid,
        "actor": "alice",
        "diffs": list(diffs),
    }


def _qemu_update_diff(
    *,
    vmid: int = 101,
    node: str = "pve01",
    **payload: object,
) -> dict[str, object]:
    return {
        "op": "update",
        "kind": "qemu",
        "netbox_id": 501,
        "payload": {
            "vmid": vmid,
            "node": node,
            **payload,
        },
    }


def _patch_netbox_session():
    return patch(
        "proxbox_api.routes.intent.dispatchers.common.get_netbox_async_session",
        new=AsyncMock(return_value=object()),
    )


@pytest.fixture
def sync_async_db_override(db_engine):
    async def _override_get_async_session():
        with Session(db_engine) as session:
            yield session

    app.dependency_overrides[get_async_session] = _override_get_async_session
    yield
    app.dependency_overrides.pop(get_async_session, None)


async def _post_apply(
    body: dict[str, object],
    *,
    auth_headers: dict[str, str],
    endpoint_id: int | None = None,
):
    params = {"endpoint_id": endpoint_id} if endpoint_id is not None else None
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        return await client.post("/intent/apply", params=params, json=body)


async def _run_qemu_update(
    db_session,
    auth_headers,
    fake: _FakeProxmoxAPI,
    diff: dict[str, object],
):
    endpoint_id = _make_endpoint(db_session, allow_writes=True)
    journal = AsyncMock(return_value={"id": 1})

    with (
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_update._open_proxmox_session",
            AsyncMock(return_value=SimpleNamespace(session=fake)),
        ),
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_update.write_verb_journal_entry",
            journal,
        ),
        _patch_netbox_session(),
    ):
        return await _post_apply(_body(diff), endpoint_id=endpoint_id, auth_headers=auth_headers)


async def test_apply_qemu_update_cores_only_succeeds_with_delta(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
    db_session,
):
    fake = _FakeProxmoxAPI([_record(config={"cores": 2})])

    response = await _run_qemu_update(
        db_session,
        auth_headers,
        fake,
        _qemu_update_diff(cores=4),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["overall"] == "succeeded"
    assert body["results"][0]["status"] == "succeeded"
    assert body["results"][0]["proxmox_upid"] == "UPID:pve01:0001:update"
    assert fake.calls == [{"kind": "qemu", "node": "pve01", "vmid": 101, "body": {"cores": 4}}]


async def test_apply_qemu_update_memory_only_succeeds(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
    db_session,
):
    fake = _FakeProxmoxAPI([_record(config={"memory": 1024})])

    response = await _run_qemu_update(
        db_session,
        auth_headers,
        fake,
        _qemu_update_diff(memory_mib=2048),
    )

    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["status"] == "succeeded"
    assert fake.calls[0]["body"] == {"memory": 2048}


async def test_apply_qemu_update_tags_only_succeeds_while_running(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
    db_session,
):
    fake = _FakeProxmoxAPI([_record(status="running", config={"tags": "old;tag"})])

    response = await _run_qemu_update(
        db_session,
        auth_headers,
        fake,
        _qemu_update_diff(tags=["new", "tag"]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["status"] == "succeeded"
    assert fake.calls[0]["body"] == {"tags": "new;tag"}


async def test_apply_qemu_update_nonexistent_vmid_fails(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
    db_session,
):
    fake = _FakeProxmoxAPI([])

    response = await _run_qemu_update(
        db_session,
        auth_headers,
        fake,
        _qemu_update_diff(vmid=999, cores=4),
    )

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "failed"
    assert result["reason"] == "toctou_mismatch"
    assert fake.calls == []


async def test_apply_qemu_update_toctou_node_mismatch_fails(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
    db_session,
):
    fake = _FakeProxmoxAPI([_record(node="pve02", config={"cores": 2})])

    response = await _run_qemu_update(
        db_session,
        auth_headers,
        fake,
        _qemu_update_diff(node="pve01", cores=4),
    )

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "failed"
    assert result["message"] == (
        "TOCTOU mismatch: vmid 101 was on node 'pve01' at plan, now on 'pve02'"
    )
    assert fake.calls == []


async def test_apply_qemu_update_writes_disabled_returns_403(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
    db_session,
):
    endpoint_id = _make_endpoint(db_session, allow_writes=False)
    open_session = AsyncMock()
    journal = AsyncMock(return_value={"id": 1})

    with (
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_update._open_proxmox_session",
            open_session,
        ),
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_update.write_verb_journal_entry",
            journal,
        ),
        _patch_netbox_session(),
    ):
        response = await _post_apply(
            _body(_qemu_update_diff(cores=4)),
            endpoint_id=endpoint_id,
            auth_headers=auth_headers,
        )

    assert response.status_code == 403, response.text
    assert response.json()["reason"] == "writes_disabled_for_endpoint"
    open_session.assert_not_awaited()
