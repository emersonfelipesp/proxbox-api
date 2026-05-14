"""Tests for CREATE-only ``POST /intent/apply``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session

from proxbox_api.database import ProxmoxEndpoint, get_async_session
from proxbox_api.main import app


class _FakeCreateEndpoint:
    def __init__(self, calls: list[dict[str, object]], upid: str) -> None:
        self._calls = calls
        self._upid = upid

    async def post(self, **kwargs):
        self._calls.append(kwargs)
        return self._upid


class _FakeNode:
    def __init__(self, calls: list[dict[str, object]], upid: str) -> None:
        self.qemu = _FakeCreateEndpoint(calls, upid)
        self.lxc = _FakeCreateEndpoint(calls, upid)


class _FakeNodes:
    def __init__(self, calls: list[dict[str, object]], upid: str) -> None:
        self._calls = calls
        self._upid = upid
        self.selected_node: str | None = None

    def __call__(self, node: str) -> _FakeNode:
        self.selected_node = node
        return _FakeNode(self._calls, self._upid)


class _FakeProxmoxAPI:
    def __init__(self, calls: list[dict[str, object]], upid: str) -> None:
        self.nodes = _FakeNodes(calls, upid)


def _make_endpoint(db_session: Session, *, allow_writes: bool) -> int:
    endpoint = ProxmoxEndpoint(
        name=f"pve-apply-{allow_writes}",
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


def _qemu_diff(vmid: int = 101) -> dict[str, object]:
    return {
        "op": "create",
        "kind": "qemu",
        "netbox_id": 501,
        "payload": {
            "vmid": vmid,
            "node": "pve01",
            "name": f"vm-{vmid}",
            "memory_mib": 1024,
        },
    }


def _body(*diffs: dict[str, object], run_uuid: str = "run-apply") -> dict[str, object]:
    return {
        "run_uuid": run_uuid,
        "actor": "alice",
        "diffs": list(diffs),
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


async def test_apply_empty_diffs_returns_no_op(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
):
    response = await _post_apply(_body(), auth_headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "run_uuid": "run-apply",
        "overall": "no_op",
        "results": [],
    }


async def test_apply_qemu_create_succeeds_with_writes_enabled(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
    db_session,
):
    endpoint_id = _make_endpoint(db_session, allow_writes=True)
    calls: list[dict[str, object]] = []
    open_session = AsyncMock(
        return_value=SimpleNamespace(session=_FakeProxmoxAPI(calls, "UPID:pve01:0001:create"))
    )
    journal = AsyncMock(return_value={"id": 1})

    with (
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_create._open_proxmox_session",
            open_session,
        ),
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_create.write_verb_journal_entry",
            journal,
        ),
        _patch_netbox_session(),
    ):
        response = await _post_apply(
            _body(_qemu_diff()),
            endpoint_id=endpoint_id,
            auth_headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["overall"] == "succeeded"
    result = body["results"][0]
    assert result["status"] == "succeeded"
    assert result["proxmox_upid"] == "UPID:pve01:0001:create"
    assert calls[0]["memory"] == 1024


async def test_apply_qemu_create_fails_when_writes_disabled(
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
            "proxbox_api.routes.intent.dispatchers.qemu_create._open_proxmox_session",
            open_session,
        ),
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_create.write_verb_journal_entry",
            journal,
        ),
        _patch_netbox_session(),
    ):
        response = await _post_apply(
            _body(_qemu_diff()),
            endpoint_id=endpoint_id,
            auth_headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["overall"] == "failed"
    assert body["results"][0]["status"] == "failed"
    assert "writes_disabled_for_endpoint" in body["results"][0]["message"]
    open_session.assert_not_awaited()


async def test_apply_lxc_create_missing_ostemplate_fails(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
    db_session,
):
    endpoint_id = _make_endpoint(db_session, allow_writes=True)
    journal = AsyncMock(return_value={"id": 1})
    lxc_diff = {
        "op": "create",
        "kind": "lxc",
        "netbox_id": 601,
        "payload": {
            "vmid": 201,
            "node": "pve01",
            "hostname": "ct-201",
        },
    }

    with (
        patch(
            "proxbox_api.routes.intent.dispatchers.lxc_create.write_verb_journal_entry",
            journal,
        ),
        _patch_netbox_session(),
    ):
        response = await _post_apply(
            _body(lxc_diff),
            endpoint_id=endpoint_id,
            auth_headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["overall"] == "failed"
    assert body["results"][0]["status"] == "failed"
    assert body["results"][0]["message"] == "ostemplate required for LXC create"


async def test_apply_update_returns_not_implemented(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
):
    update_diff = _qemu_diff()
    update_diff["op"] = "update"

    response = await _post_apply(_body(update_diff), auth_headers=auth_headers)

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "not_implemented"
    assert "Sub-PR G" in result["message"]


async def test_apply_delete_returns_deletion_request_not_implemented(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
):
    delete_diff = _qemu_diff()
    delete_diff["op"] = "delete"

    response = await _post_apply(_body(delete_diff), auth_headers=auth_headers)

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "not_implemented"
    assert "DeletionRequest" in result["message"]


async def test_apply_qemu_create_writes_success_journal(
    auth_headers,
    client_with_fake_netbox,
    sync_async_db_override,
    db_session,
):
    endpoint_id = _make_endpoint(db_session, allow_writes=True)
    calls: list[dict[str, object]] = []
    journal = AsyncMock(return_value={"id": 1})

    with (
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_create._open_proxmox_session",
            AsyncMock(
                return_value=SimpleNamespace(
                    session=_FakeProxmoxAPI(calls, "UPID:pve01:0002:create")
                )
            ),
        ),
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_create.write_verb_journal_entry",
            journal,
        ),
        _patch_netbox_session(),
    ):
        response = await _post_apply(
            _body(_qemu_diff(104), run_uuid="run-journal"),
            endpoint_id=endpoint_id,
            auth_headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    journal.assert_awaited_once()
    comments = journal.call_args.kwargs["comments"]
    assert "verb: intent_create_qemu" in comments
    assert "result: succeeded" in comments
    assert "target_vmid: 104" in comments
    assert "run_uuid: run-journal" in comments
