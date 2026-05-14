"""Tests for ``/intent/deletion-requests`` approval and execute routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlmodel import Session

from proxbox_api.database import DeletionRequestRecord, ProxmoxEndpoint
from proxbox_api.main import app


class _FakeProxmoxAPI:
    def __init__(self, upid: str) -> None:
        self.upid = upid
        self.calls: list[dict[str, object]] = []

    async def delete(self, *path_args: str, **params: object) -> dict[str, object]:
        self.calls.append({"path_args": path_args, "params": params})
        return {"data": self.upid}


def _make_deletion_request(
    db_session: Session,
    *,
    allow_writes: bool,
    state: str = "pending",
    kind: str = "qemu",
    vmid: int = 301,
    node: str = "pve01",
) -> int:
    endpoint = ProxmoxEndpoint(
        name=f"pve-delete-{kind}-{vmid}-{allow_writes}-{state}",
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

    request = DeletionRequestRecord(
        endpoint_id=endpoint.id,
        vmid=vmid,
        node=node,
        kind=kind,
        state=state,
    )
    db_session.add(request)
    db_session.commit()
    db_session.refresh(request)
    assert request.id is not None
    request_id = request.id
    db_session.close()
    return request_id


def _get_deletion_request_state(db_engine, request_id: int) -> str:
    with Session(db_engine) as session:
        record = session.get(DeletionRequestRecord, request_id)
        assert record is not None
        return record.state


def _patch_netbox_session():
    return patch(
        "proxbox_api.routes.intent.dispatchers.common.get_netbox_async_session",
        new=AsyncMock(return_value=object()),
    )


async def _post(
    path: str,
    body: dict[str, object],
    *,
    auth_headers: dict[str, str],
):
    headers = {**auth_headers, "X-Proxbox-Actor": "alice@netbox"}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        return await client.post(path, json=body)


async def test_deletion_request_approve_marks_record_approved(
    auth_headers,
    client_with_fake_netbox,
    db_engine,
    db_session,
):
    request_id = _make_deletion_request(db_session, allow_writes=True)
    journal = AsyncMock(return_value={"id": 1})

    with (
        patch("proxbox_api.routes.intent.deletion_requests.write_verb_journal_entry", journal),
        _patch_netbox_session(),
    ):
        response = await _post(
            f"/intent/deletion-requests/{request_id}/approve",
            {"vmid": 301, "node": "pve01", "kind": "qemu"},
            auth_headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "approved"
    assert _get_deletion_request_state(db_engine, request_id) == "approved"
    journal.assert_awaited_once()
    comments = journal.call_args.kwargs["comments"]
    assert "verb: deletion_request_approve" in comments
    assert "actor: alice@netbox" in comments


async def test_deletion_request_reject_marks_record_rejected(
    auth_headers,
    client_with_fake_netbox,
    db_engine,
    db_session,
):
    request_id = _make_deletion_request(db_session, allow_writes=True)
    journal = AsyncMock(return_value={"id": 1})

    with (
        patch("proxbox_api.routes.intent.deletion_requests.write_verb_journal_entry", journal),
        _patch_netbox_session(),
    ):
        response = await _post(
            f"/intent/deletion-requests/{request_id}/reject",
            {"reason": "operator canceled"},
            auth_headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "rejected"
    assert _get_deletion_request_state(db_engine, request_id) == "rejected"
    comments = journal.call_args.kwargs["comments"]
    assert "verb: deletion_request_reject" in comments
    assert "reason: operator canceled" in comments


async def test_deletion_request_execute_qemu_dispatches_destroy(
    auth_headers,
    client_with_fake_netbox,
    db_engine,
    db_session,
):
    request_id = _make_deletion_request(db_session, allow_writes=True, state="approved")
    fake = _FakeProxmoxAPI("UPID:pve01:0001:qemu-destroy")
    open_session = AsyncMock(return_value=SimpleNamespace(session=fake))
    journal = AsyncMock(return_value={"id": 1})

    with (
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_destroy._open_proxmox_session",
            open_session,
        ),
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_destroy.write_verb_journal_entry",
            journal,
        ),
        _patch_netbox_session(),
    ):
        response = await _post(
            f"/intent/deletion-requests/{request_id}/execute",
            {"vmid": 301, "node": "pve01", "kind": "qemu"},
            auth_headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["upid"] == "UPID:pve01:0001:qemu-destroy"
    assert body["run_uuid"]
    assert fake.calls == [
        {
            "path_args": ("nodes", "pve01", "qemu", "301"),
            "params": {"purge": 1},
        }
    ]
    assert _get_deletion_request_state(db_engine, request_id) == "succeeded"
    comments = journal.call_args.kwargs["comments"]
    assert "verb: deletion_request_execute" in comments
    assert "target_kind: qemu" in comments


async def test_deletion_request_execute_lxc_dispatches_destroy(
    auth_headers,
    client_with_fake_netbox,
    db_engine,
    db_session,
):
    request_id = _make_deletion_request(
        db_session,
        allow_writes=True,
        state="approved",
        kind="lxc",
        vmid=401,
    )
    fake = _FakeProxmoxAPI("UPID:pve01:0002:lxc-destroy")
    open_session = AsyncMock(return_value=SimpleNamespace(session=fake))
    journal = AsyncMock(return_value={"id": 1})

    with (
        patch(
            "proxbox_api.routes.intent.dispatchers.lxc_destroy._open_proxmox_session",
            open_session,
        ),
        patch(
            "proxbox_api.routes.intent.dispatchers.lxc_destroy.write_verb_journal_entry",
            journal,
        ),
        _patch_netbox_session(),
    ):
        response = await _post(
            f"/intent/deletion-requests/{request_id}/execute",
            {"vmid": 401, "node": "pve01", "kind": "lxc"},
            auth_headers=auth_headers,
        )

    assert response.status_code == 200, response.text
    assert response.json()["upid"] == "UPID:pve01:0002:lxc-destroy"
    assert fake.calls == [
        {
            "path_args": ("nodes", "pve01", "lxc", "401"),
            "params": {"purge": 1},
        }
    ]
    assert _get_deletion_request_state(db_engine, request_id) == "succeeded"
    assert "target_kind: lxc" in journal.call_args.kwargs["comments"]


async def test_deletion_request_execute_writes_disabled_returns_403(
    auth_headers,
    client_with_fake_netbox,
    db_engine,
    db_session,
):
    request_id = _make_deletion_request(db_session, allow_writes=False, state="approved")
    open_session = AsyncMock()
    journal = AsyncMock(return_value={"id": 1})

    with (
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_destroy._open_proxmox_session",
            open_session,
        ),
        patch("proxbox_api.routes.intent.deletion_requests.write_verb_journal_entry", journal),
        _patch_netbox_session(),
    ):
        response = await _post(
            f"/intent/deletion-requests/{request_id}/execute",
            {"vmid": 301, "node": "pve01", "kind": "qemu"},
            auth_headers=auth_headers,
        )

    assert response.status_code == 403, response.text
    assert response.json()["reason"] == "endpoint_writes_disabled"
    open_session.assert_not_awaited()
    assert _get_deletion_request_state(db_engine, request_id) == "approved"
    assert "verb: deletion_request_execute" in journal.call_args.kwargs["comments"]
