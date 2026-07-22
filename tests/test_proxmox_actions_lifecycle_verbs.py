"""Tests for VM lifecycle verb endpoints added after start/stop/snapshot/migrate."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxboxException, ProxmoxAPIError
from proxbox_api.routes import proxmox_actions
from proxbox_api.services.idempotency import get_idempotency_cache

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clear_idempotency_cache():
    await get_idempotency_cache().clear()
    yield
    await get_idempotency_cache().clear()


class _GateSession:
    def __init__(self, endpoint: ProxmoxEndpoint) -> None:
        self.endpoint = endpoint

    async def get(self, model: object, object_id: int) -> ProxmoxEndpoint | None:
        if model is ProxmoxEndpoint and object_id == self.endpoint.id:
            return self.endpoint
        return None


class _FakeDestroyAPI:
    def __init__(self, upid: str) -> None:
        self.upid = upid
        self.calls: list[dict[str, object]] = []

    async def delete(self, *path_args: str, **params: object) -> dict[str, object]:
        self.calls.append({"path_args": path_args, "params": params})
        return {"data": self.upid}


@pytest.fixture
def route_session() -> _GateSession:
    return _GateSession(
        ProxmoxEndpoint(
            id=73,
            name="pve-prod",
            ip_address="10.0.0.10",
            port=8006,
            username="root@pam",
            verify_ssl=False,
            allow_writes=True,
        )
    )


def _endpoint_id(session: _GateSession) -> int:
    endpoint_id = session.endpoint.id
    assert endpoint_id is not None
    return endpoint_id


def _json_response(response) -> dict[str, object]:
    return json.loads(response.body)


async def _call_reboot(
    session: _GateSession,
    *,
    vm_type: proxmox_actions.VmType = "qemu",
    vmid: int = 100,
    idempotency_key: str | None = None,
    actor: str = "proxbox-api",
):
    return await proxmox_actions._handle_reboot(
        vm_type,
        vmid,
        session,  # type: ignore[arg-type]
        _endpoint_id(session),
        idempotency_key,
        actor,
    )


async def _call_delete(
    session: _GateSession,
    *,
    vm_type: proxmox_actions.VmType = "qemu",
    vmid: int = 100,
    idempotency_key: str | None = None,
    actor: str = "proxbox-api",
):
    return await proxmox_actions._handle_delete(
        vm_type,
        vmid,
        session,  # type: ignore[arg-type]
        _endpoint_id(session),
        idempotency_key,
        actor,
    )


async def _call_backup(
    session: _GateSession,
    *,
    vm_type: proxmox_actions.VmType = "qemu",
    vmid: int = 100,
    body: proxmox_actions.BackupRequest | None,
    idempotency_key: str | None = None,
    actor: str = "proxbox-api",
):
    return await proxmox_actions._handle_backup(
        vm_type,
        vmid,
        session,  # type: ignore[arg-type]
        _endpoint_id(session),
        idempotency_key,
        actor,
        body,
    )


async def _call_delete_snapshot(
    session: _GateSession,
    *,
    vm_type: proxmox_actions.VmType = "qemu",
    vmid: int = 100,
    snapname: str = "pre-upgrade",
    idempotency_key: str | None = None,
    actor: str = "proxbox-api",
):
    return await proxmox_actions._handle_delete_snapshot(
        vm_type,
        vmid,
        snapname,
        session,  # type: ignore[arg-type]
        _endpoint_id(session),
        idempotency_key,
        actor,
    )


@contextmanager
def _patched_route(
    *,
    node_or_response="pve-node-01",
    netbox_vm_id: int | None = 42,
    netbox_id_side_effect=None,
    status_payload=SimpleNamespace(status="running"),
    reboot_result="UPID:pve-node-01:0001:reboot",
    reboot_side_effect=None,
    stop_result="UPID:pve-node-01:0001:stop",
    stop_side_effect=None,
    delete_result="UPID:pve-node-01:0001:delete",
    delete_side_effect=None,
    backup_result="UPID:pve-node-01:0001:vzdump",
    backup_side_effect=None,
    delete_snapshot_result="UPID:pve-node-01:0001:delsnap",
    delete_snapshot_side_effect=None,
    journal_entry: dict | None = None,
    journal_create_side_effect=None,
    journal_update_side_effect=None,
):
    if journal_entry is None:
        journal_entry = {"id": 793, "url": "/api/extras/journal-entries/793/"}

    handles = {
        "open_session": AsyncMock(return_value=object()),
        "nb_session": AsyncMock(return_value=object()),
        "node": AsyncMock(return_value=node_or_response),
        "netbox_id": AsyncMock(return_value=netbox_vm_id, side_effect=netbox_id_side_effect),
        "status": AsyncMock(return_value=status_payload),
        "reboot": AsyncMock(return_value=reboot_result, side_effect=reboot_side_effect),
        "stop": AsyncMock(return_value=stop_result, side_effect=stop_side_effect),
        "delete": AsyncMock(return_value=delete_result, side_effect=delete_side_effect),
        "backup": AsyncMock(return_value=backup_result, side_effect=backup_side_effect),
        "delete_snapshot": AsyncMock(
            return_value=delete_snapshot_result,
            side_effect=delete_snapshot_side_effect,
        ),
        "journal_create": AsyncMock(
            return_value=journal_entry,
            side_effect=journal_create_side_effect,
        ),
        "journal": AsyncMock(
            return_value=journal_entry,
            side_effect=journal_update_side_effect,
        ),
    }
    patches = [
        patch("proxbox_api.routes.proxmox_actions._open_proxmox_session", handles["open_session"]),
        patch("proxbox_api.routes.proxmox_actions.get_netbox_async_session", handles["nb_session"]),
        patch("proxbox_api.routes.proxmox_actions.resolve_proxmox_node", handles["node"]),
        patch("proxbox_api.routes.proxmox_actions.resolve_netbox_vm_id", handles["netbox_id"]),
        patch("proxbox_api.routes.proxmox_actions.get_vm_status", handles["status"]),
        patch("proxbox_api.routes.proxmox_actions.reboot_vm", handles["reboot"]),
        patch("proxbox_api.routes.proxmox_actions.stop_vm", handles["stop"]),
        patch(
            "proxbox_api.routes.proxmox_actions.delete_vm_via_intent_dispatcher",
            handles["delete"],
        ),
        patch("proxbox_api.routes.proxmox_actions.backup_vm", handles["backup"]),
        patch("proxbox_api.routes.proxmox_actions.delete_vm_snapshot", handles["delete_snapshot"]),
        patch(
            "proxbox_api.routes.proxmox_actions.write_verb_journal_entry",
            handles["journal_create"],
        ),
        patch("proxbox_api.routes.proxmox_actions.update_verb_journal_entry", handles["journal"]),
    ]
    started = [patcher.start() for patcher in patches]
    try:
        yield handles
    finally:
        for patcher in reversed(patches):
            patcher.stop()
        started.clear()


async def test_reboot_qemu_success_returns_response_shape_and_writes_journal(
    route_session: _GateSession,
):
    endpoint_id = _endpoint_id(route_session)
    with _patched_route() as handles:
        resp = await _call_reboot(
            route_session,
            idempotency_key="reboot-key-1",
            actor="alice@netbox",
        )

    assert resp.status_code == 200
    body = _json_response(resp)
    assert body["verb"] == "reboot"
    assert body["vmid"] == 100
    assert body["vm_type"] == "qemu"
    assert body["endpoint_id"] == endpoint_id
    assert body["result"] == "ok"
    assert body["proxmox_task_upid"] == "UPID:pve-node-01:0001:reboot"
    assert body["journal_entry_url"] == "/api/extras/journal-entries/793/"
    handles["reboot"].assert_awaited_once()
    handles["journal"].assert_awaited_once()
    assert "verb: reboot" in handles["journal"].call_args.kwargs["comments"]
    assert "actor: alice@netbox" in handles["journal"].call_args.kwargs["comments"]


async def test_reboot_qemu_creates_writeahead_journal_before_dispatch(
    route_session: _GateSession,
):
    events: list[str] = []

    async def _create_journal(_nb, **kwargs):
        events.append("journal_create")
        assert kwargs["kind"] == "info"
        assert "result: in_progress" in kwargs["comments"]
        return {"id": 793, "url": "/api/extras/journal-entries/793/"}

    async def _reboot_vm(*_args, **_kwargs):
        events.append("reboot_dispatch")
        return "UPID:pve-node-01:0001:reboot"

    with _patched_route(
        reboot_side_effect=_reboot_vm,
        journal_create_side_effect=_create_journal,
    ) as handles:
        resp = await _call_reboot(route_session)

    assert resp.status_code == 200
    assert events.index("journal_create") < events.index("reboot_dispatch")
    handles["journal_create"].assert_awaited_once()
    handles["journal"].assert_awaited_once()


async def test_reboot_qemu_writeahead_journal_without_id_blocks_dispatch(
    route_session: _GateSession,
):
    with _patched_route(journal_entry={"url": "/api/extras/journal-entries/no-id/"}) as handles:
        resp = await _call_reboot(route_session)

    assert resp.status_code == 409
    body = _json_response(resp)
    assert body["reason"] == "netbox_vm_identity_required_for_audit"
    assert body["verb"] == "reboot"
    handles["journal_create"].assert_awaited_once()
    handles["status"].assert_not_awaited()
    handles["reboot"].assert_not_awaited()
    handles["journal"].assert_not_awaited()


async def test_reboot_qemu_idempotency_key_reuse_returns_cached_response(
    route_session: _GateSession,
):
    with _patched_route() as handles:
        r1 = await _call_reboot(route_session, idempotency_key="reboot-reuse-1")
        r2 = await _call_reboot(route_session, idempotency_key="reboot-reuse-1")

    assert r1.status_code == 200 and r2.status_code == 200
    assert _json_response(r1) == _json_response(r2)
    assert handles["reboot"].await_count == 1
    assert handles["journal"].await_count == 1


async def test_reboot_qemu_already_stopped_skips_dispatch_but_writes_journal(
    route_session: _GateSession,
):
    with _patched_route(status_payload=SimpleNamespace(status="stopped")) as handles:
        resp = await _call_reboot(route_session)

    assert resp.status_code == 200
    body = _json_response(resp)
    assert body["result"] == "already_stopped"
    assert "proxmox_task_upid" not in body
    handles["reboot"].assert_not_awaited()
    handles["journal"].assert_awaited_once()


async def test_reboot_qemu_dispatch_failure_writes_warning_journal(
    route_session: _GateSession,
):
    with _patched_route(
        reboot_side_effect=ProxmoxAPIError(message="guest agent timeout")
    ) as handles:
        resp = await _call_reboot(route_session)

    assert resp.status_code == 502
    body = _json_response(resp)
    assert body["result"] == "failed"
    assert body["reason"] == "proxmox_dispatch_failed"
    assert "guest agent timeout" in body["detail"]
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"


async def test_reboot_lxc_routes_through_same_dispatch(route_session: _GateSession):
    with _patched_route() as handles:
        resp = await _call_reboot(route_session, vm_type="lxc", vmid=101)

    assert resp.status_code == 200
    body = _json_response(resp)
    assert body["vm_type"] == "lxc"
    assert body["vmid"] == 101
    node_call = handles["node"].call_args
    assert node_call.args[1] == "lxc" or node_call.kwargs.get("vm_type") == "lxc"


async def test_delete_qemu_running_stops_then_deletes_and_audits_once(
    route_session: _GateSession,
):
    with _patched_route() as handles:
        resp = await _call_delete(
            route_session,
            idempotency_key="delete-key-1",
            actor="alice@netbox",
        )

    assert resp.status_code == 200
    body = _json_response(resp)
    assert body["verb"] == "delete"
    assert body["result"] == "ok"
    assert body["proxmox_task_upid"] == "UPID:pve-node-01:0001:delete"
    assert body["stop_task_upid"] == "UPID:pve-node-01:0001:stop"
    handles["stop"].assert_awaited_once()
    handles["delete"].assert_awaited_once()
    assert handles["delete"].call_args.kwargs["suppress_dispatcher_journal"] is True
    handles["journal"].assert_awaited_once()
    assert "verb: delete" in handles["journal"].call_args.kwargs["comments"]


async def test_delete_qemu_creates_writeahead_journal_before_stop_and_delete(
    route_session: _GateSession,
):
    events: list[str] = []

    async def _create_journal(_nb, **kwargs):
        events.append("journal_create")
        assert kwargs["kind"] == "info"
        assert "result: in_progress" in kwargs["comments"]
        return {"id": 793, "url": "/api/extras/journal-entries/793/"}

    async def _stop_vm(*_args, **_kwargs):
        events.append("stop_dispatch")
        return "UPID:pve-node-01:0001:stop"

    async def _delete_vm(*_args, **_kwargs):
        events.append("delete_dispatch")
        return "UPID:pve-node-01:0001:delete"

    with _patched_route(
        stop_side_effect=_stop_vm,
        delete_side_effect=_delete_vm,
        journal_create_side_effect=_create_journal,
    ) as handles:
        resp = await _call_delete(route_session)

    assert resp.status_code == 200
    assert events.index("journal_create") < events.index("stop_dispatch")
    assert events.index("journal_create") < events.index("delete_dispatch")
    handles["journal_create"].assert_awaited_once()
    handles["journal"].assert_awaited_once()


async def test_delete_qemu_writeahead_journal_without_id_blocks_dispatch(
    route_session: _GateSession,
):
    with _patched_route(journal_entry={"url": "/api/extras/journal-entries/no-id/"}) as handles:
        resp = await _call_delete(route_session)

    assert resp.status_code == 409
    body = _json_response(resp)
    assert body["reason"] == "netbox_vm_identity_required_for_audit"
    assert body["verb"] == "delete"
    handles["journal_create"].assert_awaited_once()
    handles["status"].assert_not_awaited()
    handles["stop"].assert_not_awaited()
    handles["delete"].assert_not_awaited()
    handles["journal"].assert_not_awaited()


async def test_delete_qemu_unresolved_netbox_vm_fails_closed_before_dispatch(
    route_session: _GateSession,
):
    with _patched_route(netbox_vm_id=None) as handles:
        resp = await _call_delete(
            route_session,
            idempotency_key="delete-no-audit-target",
            actor="alice@netbox",
        )

    assert resp.status_code == 409
    body = _json_response(resp)
    assert body["reason"] == "netbox_vm_identity_required_for_audit"
    assert body["verb"] == "delete"
    assert body["vmid"] == 100
    assert body["endpoint_id"] == _endpoint_id(route_session)
    handles["status"].assert_not_awaited()
    handles["stop"].assert_not_awaited()
    handles["delete"].assert_not_awaited()
    handles["journal"].assert_not_awaited()


async def test_delete_qemu_ambiguous_netbox_vm_fails_closed_before_dispatch(
    route_session: _GateSession,
):
    with _patched_route(
        netbox_id_side_effect=ProxboxException(
            message="Refusing to create or bind a VM from ambiguous sync-state identity.",
            detail={
                "reason": "netbox_vm_identity_unverifiable_for_audit",
                "vmid": 100,
                "endpoint_id": _endpoint_id(route_session),
            },
            http_status_code=409,
        )
    ) as handles:
        resp = await _call_delete(route_session)

    assert resp.status_code == 409
    body = _json_response(resp)
    assert body["reason"] == "netbox_vm_identity_required_for_audit"
    assert body["verb"] == "delete"
    handles["status"].assert_not_awaited()
    handles["stop"].assert_not_awaited()
    handles["delete"].assert_not_awaited()
    handles["journal"].assert_not_awaited()


async def test_delete_qemu_idempotency_key_reuse_returns_cached_response(
    route_session: _GateSession,
):
    with _patched_route() as handles:
        r1 = await _call_delete(route_session, idempotency_key="delete-reuse-1")
        r2 = await _call_delete(route_session, idempotency_key="delete-reuse-1")

    assert r1.status_code == 200 and r2.status_code == 200
    assert _json_response(r1) == _json_response(r2)
    assert handles["stop"].await_count == 1
    assert handles["delete"].await_count == 1
    assert handles["journal"].await_count == 1


async def test_delete_qemu_stopped_skips_stop_and_deletes(route_session: _GateSession):
    with _patched_route(status_payload=SimpleNamespace(status="stopped")) as handles:
        resp = await _call_delete(route_session)

    assert resp.status_code == 200
    body = _json_response(resp)
    assert body["result"] == "ok"
    assert "stop_task_upid" not in body
    handles["stop"].assert_not_awaited()
    handles["delete"].assert_awaited_once()
    handles["journal"].assert_awaited_once()


async def test_delete_qemu_stop_failure_writes_warning_and_skips_delete(
    route_session: _GateSession,
):
    with _patched_route(stop_side_effect=ProxmoxAPIError(message="stop refused")) as handles:
        resp = await _call_delete(route_session)

    assert resp.status_code == 502
    body = _json_response(resp)
    assert body["result"] == "failed"
    assert body["reason"] == "proxmox_dispatch_failed"
    assert "stop refused" in body["detail"]
    handles["delete"].assert_not_awaited()
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"


async def test_delete_qemu_delete_failure_writes_warning_journal(
    route_session: _GateSession,
):
    with _patched_route(
        status_payload=SimpleNamespace(status="stopped"),
        delete_side_effect=ProxmoxAPIError(message="vm is locked"),
    ) as handles:
        resp = await _call_delete(route_session)

    assert resp.status_code == 502
    body = _json_response(resp)
    assert body["result"] == "failed"
    assert body["reason"] == "proxmox_dispatch_failed"
    assert "vm is locked" in body["detail"]
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"


async def test_delete_lxc_routes_through_same_dispatch(route_session: _GateSession):
    with _patched_route(status_payload=SimpleNamespace(status="stopped")) as handles:
        resp = await _call_delete(route_session, vm_type="lxc", vmid=101)

    assert resp.status_code == 200
    body = _json_response(resp)
    assert body["vm_type"] == "lxc"
    assert body["vmid"] == 101
    node_call = handles["node"].call_args
    assert node_call.args[1] == "lxc" or node_call.kwargs.get("vm_type") == "lxc"


@pytest.mark.parametrize(
    ("module_name", "dispatcher_name", "kind"),
    [
        ("proxbox_api.routes.intent.dispatchers.qemu_destroy", "dispatch_qemu_destroy", "qemu"),
        ("proxbox_api.routes.intent.dispatchers.lxc_destroy", "dispatch_lxc_destroy", "lxc"),
    ],
)
async def test_destroy_dispatcher_suppresses_nested_journal_when_requested(
    route_session: _GateSession,
    module_name: str,
    dispatcher_name: str,
    kind: str,
):
    from proxbox_api.routes.intent.dispatchers.common import IntentEndpointContext

    module = import_module(module_name)
    dispatcher = getattr(module, dispatcher_name)
    fake_api = _FakeDestroyAPI(f"UPID:pve-node-01:0001:{kind}-destroy")
    journal = AsyncMock()

    with (
        patch(f"{module_name}._gate", new=AsyncMock(return_value=route_session.endpoint)),
        patch(
            f"{module_name}._open_proxmox_session",
            new=AsyncMock(return_value=SimpleNamespace(session=fake_api)),
        ),
        patch(f"{module_name}.write_deletion_request_journal", journal),
    ):
        result = await dispatcher(
            IntentEndpointContext(
                session=route_session,
                endpoint_id=_endpoint_id(route_session),
            ),
            100,
            "pve-node-01",
            "run-1",
            actor="alice@netbox",
            suppress_journal=True,
        )

    assert result["upid"] == f"UPID:pve-node-01:0001:{kind}-destroy"
    assert fake_api.calls == [
        {
            "path_args": ("nodes", "pve-node-01", kind, "100"),
            "params": {"purge": 1},
        }
    ]
    journal.assert_not_awaited()


async def test_backup_qemu_success_forwards_vzdump_body_and_writes_journal(
    route_session: _GateSession,
):
    with _patched_route() as handles:
        resp = await _call_backup(
            route_session,
            idempotency_key="backup-key-1",
            actor="alice@netbox",
            body=proxmox_actions.BackupRequest(
                storage="pbs-main",
                mode="snapshot",
                compress="zstd",
                notes="manual backup",
            ),
        )

    assert resp.status_code == 200
    body = _json_response(resp)
    assert body["verb"] == "backup"
    assert body["result"] == "ok"
    assert body["proxmox_task_upid"] == "UPID:pve-node-01:0001:vzdump"
    assert body["storage"] == "pbs-main"
    assert body["mode"] == "snapshot"
    assert body["compress"] == "zstd"
    assert body["notes"] == "manual backup"
    handles["backup"].assert_awaited_once()
    backup_kwargs = handles["backup"].call_args.kwargs
    assert backup_kwargs == {
        "storage": "pbs-main",
        "mode": "snapshot",
        "compress": "zstd",
        "notes": "manual backup",
    }
    handles["journal"].assert_awaited_once()
    assert "verb: backup" in handles["journal"].call_args.kwargs["comments"]


async def test_backup_qemu_creates_writeahead_journal_before_dispatch(
    route_session: _GateSession,
):
    events: list[str] = []

    async def _create_journal(_nb, **kwargs):
        events.append("journal_create")
        assert kwargs["kind"] == "info"
        assert "result: in_progress" in kwargs["comments"]
        return {"id": 793, "url": "/api/extras/journal-entries/793/"}

    async def _backup_vm(*_args, **_kwargs):
        events.append("backup_dispatch")
        return "UPID:pve-node-01:0001:vzdump"

    with _patched_route(
        backup_side_effect=_backup_vm,
        journal_create_side_effect=_create_journal,
    ) as handles:
        resp = await _call_backup(
            route_session,
            body=proxmox_actions.BackupRequest(storage="pbs-main"),
        )

    assert resp.status_code == 200
    assert events.index("journal_create") < events.index("backup_dispatch")
    handles["journal_create"].assert_awaited_once()
    handles["journal"].assert_awaited_once()


async def test_backup_qemu_writeahead_journal_without_id_blocks_dispatch(
    route_session: _GateSession,
):
    with _patched_route(journal_entry={"url": "/api/extras/journal-entries/no-id/"}) as handles:
        resp = await _call_backup(
            route_session,
            body=proxmox_actions.BackupRequest(storage="pbs-main"),
        )

    assert resp.status_code == 409
    body = _json_response(resp)
    assert body["reason"] == "netbox_vm_identity_required_for_audit"
    assert body["verb"] == "backup"
    handles["journal_create"].assert_awaited_once()
    handles["backup"].assert_not_awaited()
    handles["journal"].assert_not_awaited()


async def test_backup_qemu_cancelled_dispatch_finalizes_writeahead_then_reraises(
    route_session: _GateSession,
):
    with _patched_route(backup_side_effect=asyncio.CancelledError()) as handles:
        with pytest.raises(asyncio.CancelledError):
            await _call_backup(
                route_session,
                body=proxmox_actions.BackupRequest(storage="pbs-main"),
            )

    handles["journal_create"].assert_awaited_once()
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"
    assert "result: interrupted" in handles["journal"].call_args.kwargs["comments"]


async def test_backup_qemu_idempotency_key_reuse_returns_cached_response(
    route_session: _GateSession,
):
    with _patched_route() as handles:
        r1 = await _call_backup(
            route_session,
            idempotency_key="backup-reuse-1",
            body=proxmox_actions.BackupRequest(storage="pbs-main"),
        )
        r2 = await _call_backup(
            route_session,
            idempotency_key="backup-reuse-1",
            body=proxmox_actions.BackupRequest(storage="pbs-main"),
        )

    assert r1.status_code == 200 and r2.status_code == 200
    assert _json_response(r1) == _json_response(r2)
    assert handles["backup"].await_count == 1
    assert handles["journal"].await_count == 1


async def test_backup_qemu_missing_storage_returns_400_after_gate(
    route_session: _GateSession,
):
    with _patched_route() as handles:
        resp = await _call_backup(route_session, body=proxmox_actions.BackupRequest())

    assert resp.status_code == 400
    body = _json_response(resp)
    assert body["reason"] == "storage_required"
    handles["nb_session"].assert_not_awaited()
    handles["backup"].assert_not_awaited()
    handles["journal"].assert_not_awaited()


async def test_backup_qemu_dispatch_failure_writes_warning_journal(
    route_session: _GateSession,
):
    with _patched_route(backup_side_effect=ProxmoxAPIError(message="backup lock held")) as handles:
        resp = await _call_backup(
            route_session,
            body=proxmox_actions.BackupRequest(storage="pbs-main"),
        )

    assert resp.status_code == 502
    body = _json_response(resp)
    assert body["result"] == "failed"
    assert body["reason"] == "proxmox_dispatch_failed"
    assert "backup lock held" in body["detail"]
    assert body["storage"] == "pbs-main"
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"


async def test_backup_lxc_routes_through_same_dispatch(route_session: _GateSession):
    with _patched_route() as handles:
        resp = await _call_backup(
            route_session,
            vm_type="lxc",
            vmid=101,
            body=proxmox_actions.BackupRequest(storage="pbs-main"),
        )

    assert resp.status_code == 200
    body = _json_response(resp)
    assert body["vm_type"] == "lxc"
    assert body["vmid"] == 101
    node_call = handles["node"].call_args
    assert node_call.args[1] == "lxc" or node_call.kwargs.get("vm_type") == "lxc"


async def test_delete_snapshot_qemu_success_returns_shape_and_writes_journal(
    route_session: _GateSession,
):
    with _patched_route() as handles:
        resp = await _call_delete_snapshot(
            route_session,
            idempotency_key="delete-snapshot-key-1",
            actor="alice@netbox",
        )

    assert resp.status_code == 200
    body = _json_response(resp)
    assert body["verb"] == "delete_snapshot"
    assert body["result"] == "ok"
    assert body["snapname"] == "pre-upgrade"
    assert body["proxmox_task_upid"] == "UPID:pve-node-01:0001:delsnap"
    handles["delete_snapshot"].assert_awaited_once()
    call_args = handles["delete_snapshot"].call_args
    assert "pre-upgrade" in call_args.args or call_args.kwargs.get("snapname") == "pre-upgrade"
    handles["journal"].assert_awaited_once()
    assert "verb: delete_snapshot" in handles["journal"].call_args.kwargs["comments"]


async def test_delete_snapshot_qemu_creates_writeahead_journal_before_dispatch(
    route_session: _GateSession,
):
    events: list[str] = []

    async def _create_journal(_nb, **kwargs):
        events.append("journal_create")
        assert kwargs["kind"] == "info"
        assert "result: in_progress" in kwargs["comments"]
        return {"id": 793, "url": "/api/extras/journal-entries/793/"}

    async def _delete_snapshot(*_args, **_kwargs):
        events.append("delete_snapshot_dispatch")
        return "UPID:pve-node-01:0001:delsnap"

    with _patched_route(
        delete_snapshot_side_effect=_delete_snapshot,
        journal_create_side_effect=_create_journal,
    ) as handles:
        resp = await _call_delete_snapshot(route_session)

    assert resp.status_code == 200
    assert events.index("journal_create") < events.index("delete_snapshot_dispatch")
    handles["journal_create"].assert_awaited_once()
    handles["journal"].assert_awaited_once()


async def test_delete_snapshot_qemu_writeahead_journal_without_id_blocks_dispatch(
    route_session: _GateSession,
):
    with _patched_route(journal_entry={"url": "/api/extras/journal-entries/no-id/"}) as handles:
        resp = await _call_delete_snapshot(route_session)

    assert resp.status_code == 409
    body = _json_response(resp)
    assert body["reason"] == "netbox_vm_identity_required_for_audit"
    assert body["verb"] == "delete_snapshot"
    handles["journal_create"].assert_awaited_once()
    handles["delete_snapshot"].assert_not_awaited()
    handles["journal"].assert_not_awaited()


async def test_delete_snapshot_qemu_idempotency_key_reuse_returns_cached_response(
    route_session: _GateSession,
):
    with _patched_route() as handles:
        r1 = await _call_delete_snapshot(
            route_session,
            idempotency_key="delete-snapshot-reuse-1",
        )
        r2 = await _call_delete_snapshot(
            route_session,
            idempotency_key="delete-snapshot-reuse-1",
        )

    assert r1.status_code == 200 and r2.status_code == 200
    assert _json_response(r1) == _json_response(r2)
    assert handles["delete_snapshot"].await_count == 1
    assert handles["journal"].await_count == 1


async def test_delete_snapshot_qemu_dispatch_failure_writes_warning_journal(
    route_session: _GateSession,
):
    with _patched_route(
        delete_snapshot_side_effect=ProxmoxAPIError(message="snapshot missing")
    ) as handles:
        resp = await _call_delete_snapshot(route_session)

    assert resp.status_code == 502
    body = _json_response(resp)
    assert body["result"] == "failed"
    assert body["reason"] == "proxmox_dispatch_failed"
    assert "snapshot missing" in body["detail"]
    assert body["snapname"] == "pre-upgrade"
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"


async def test_delete_snapshot_lxc_routes_through_same_dispatch(
    route_session: _GateSession,
):
    with _patched_route() as handles:
        resp = await _call_delete_snapshot(
            route_session,
            vm_type="lxc",
            vmid=101,
        )

    assert resp.status_code == 200
    body = _json_response(resp)
    assert body["vm_type"] == "lxc"
    assert body["vmid"] == 101
    node_call = handles["node"].call_args
    assert node_call.args[1] == "lxc" or node_call.kwargs.get("vm_type") == "lxc"
