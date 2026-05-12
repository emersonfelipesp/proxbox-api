"""Tests for shared verb-dispatch helpers (issue #376 sub-PR C).

Pins the contracts in ``docs/design/operational-verbs.md`` §6 and §7.3:

- Journal ``comments`` is a structured Markdown block with bullet keys
  the design doc commits to (``verb``, ``actor``, ``result``,
  ``proxmox_task_upid``, ``idempotency_key``, ``endpoint``,
  ``dispatched_at``).
- The non-migrate success response carries ``verb``, ``vmid``,
  ``vm_type``, ``endpoint_id``, ``result``, ``dispatched_at`` and
  optionally ``proxmox_task_upid`` / ``journal_entry_url``.
- The node resolver returns a 404 ``JSONResponse`` (not raises) when
  the VM is absent so the route can compose it with the existing 403
  gate path.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import JSONResponse

from proxbox_api.exception import ProxmoxAPIError
from proxbox_api.services import verb_dispatch


class _FakeResource:
    def __init__(self, vmid: int | None, node: str | None) -> None:
        self.vmid = vmid
        self.node = node


async def test_build_journal_comments_includes_all_keys():
    body = verb_dispatch.build_journal_comments(
        verb="start",
        actor="alice@netbox",
        result="ok",
        endpoint_name="pve-prod",
        endpoint_id=3,
        dispatched_at="2026-05-12T18:42:11Z",
        proxmox_task_upid="UPID:pve-node-01:00012F4A:00:start",
        idempotency_key="7b3c9f4a-aaaa-bbbb-cccc-deadbeefcafe",
    )
    assert "verb: start" in body
    assert "actor: alice@netbox" in body
    assert "result: ok" in body
    assert "proxmox_task_upid: UPID:pve-node-01:00012F4A:00:start" in body
    assert "idempotency_key: 7b3c9f4a-aaaa-bbbb-cccc-deadbeefcafe" in body
    assert "endpoint: pve-prod (id=3)" in body
    assert "dispatched_at: 2026-05-12T18:42:11Z" in body


async def test_build_journal_comments_omits_optional_when_absent():
    body = verb_dispatch.build_journal_comments(
        verb="start",
        actor="alice@netbox",
        result="already_running",
        endpoint_name="pve-prod",
        endpoint_id=3,
        dispatched_at="2026-05-12T18:42:11Z",
    )
    assert "proxmox_task_upid" not in body
    assert "idempotency_key" not in body
    assert "error_detail" not in body
    assert "result: already_running" in body


async def test_build_journal_comments_includes_error_detail_on_failure():
    body = verb_dispatch.build_journal_comments(
        verb="start",
        actor="alice@netbox",
        result="failed",
        endpoint_name="pve-prod",
        endpoint_id=3,
        dispatched_at="2026-05-12T18:42:11Z",
        error_detail="Proxmox returned 500: lock conflict",
    )
    assert "result: failed" in body
    assert "error_detail: Proxmox returned 500: lock conflict" in body


async def test_utcnow_iso_format():
    """ISO-8601 UTC with second precision and trailing 'Z'."""
    stamp = verb_dispatch.utcnow_iso()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", stamp)


async def test_build_success_response_shape():
    body = verb_dispatch.build_success_response(
        verb="start",
        vm_type="qemu",
        vmid=101,
        endpoint_id=3,
        result="ok",
        dispatched_at="2026-05-12T18:42:11Z",
        proxmox_task_upid="UPID:x",
        journal_entry_url="/api/extras/journal-entries/789/",
    )
    assert body == {
        "verb": "start",
        "vmid": 101,
        "vm_type": "qemu",
        "endpoint_id": 3,
        "result": "ok",
        "dispatched_at": "2026-05-12T18:42:11Z",
        "proxmox_task_upid": "UPID:x",
        "journal_entry_url": "/api/extras/journal-entries/789/",
    }


async def test_build_success_response_omits_upid_for_noop():
    """A no-op (already_running / already_stopped) carries no UPID."""
    body = verb_dispatch.build_success_response(
        verb="start",
        vm_type="qemu",
        vmid=101,
        endpoint_id=3,
        result="already_running",
        dispatched_at="2026-05-12T18:42:11Z",
    )
    assert "proxmox_task_upid" not in body
    assert "journal_entry_url" not in body
    assert body["result"] == "already_running"


async def test_resolve_proxmox_node_returns_node_on_match():
    with patch(
        "proxbox_api.services.verb_dispatch.get_cluster_resources",
        new=AsyncMock(
            return_value=[
                _FakeResource(vmid=200, node="pve-node-02"),
                _FakeResource(vmid=101, node="pve-node-01"),
            ]
        ),
    ):
        node = await verb_dispatch.resolve_proxmox_node(
            session=object(), vm_type="qemu", vmid=101
        )
    assert node == "pve-node-01"


async def test_resolve_proxmox_node_returns_404_when_absent():
    with patch(
        "proxbox_api.services.verb_dispatch.get_cluster_resources",
        new=AsyncMock(return_value=[_FakeResource(vmid=999, node="pve-node-02")]),
    ):
        result = await verb_dispatch.resolve_proxmox_node(
            session=object(), vm_type="qemu", vmid=101
        )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 404
    assert b"vm_not_found_in_cluster" in result.body


async def test_resolve_proxmox_node_returns_502_on_proxmox_error():
    with patch(
        "proxbox_api.services.verb_dispatch.get_cluster_resources",
        new=AsyncMock(side_effect=ProxmoxAPIError(message="boom")),
    ):
        result = await verb_dispatch.resolve_proxmox_node(
            session=object(), vm_type="qemu", vmid=101
        )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 502
    assert b"proxmox_cluster_resources_unreachable" in result.body


async def test_resolve_netbox_vm_id_returns_id_when_found():
    fake_record = {"id": 42, "name": "vm-fixture"}
    with patch(
        "proxbox_api.services.verb_dispatch.rest_first_async",
        new=AsyncMock(return_value=fake_record),
    ):
        netbox_id = await verb_dispatch.resolve_netbox_vm_id(nb=object(), vmid=101)
    assert netbox_id == 42


async def test_resolve_netbox_vm_id_returns_none_when_missing():
    with patch(
        "proxbox_api.services.verb_dispatch.rest_first_async",
        new=AsyncMock(return_value=None),
    ):
        netbox_id = await verb_dispatch.resolve_netbox_vm_id(nb=object(), vmid=101)
    assert netbox_id is None


async def test_write_verb_journal_entry_posts_payload():
    create_mock = AsyncMock(return_value={"id": 789, "url": "/api/extras/journal-entries/789/"})
    with patch(
        "proxbox_api.services.verb_dispatch.rest_create_async", new=create_mock
    ):
        entry = await verb_dispatch.write_verb_journal_entry(
            nb=object(),
            netbox_vm_id=42,
            kind="info",
            comments="body",
        )
    assert entry == {"id": 789, "url": "/api/extras/journal-entries/789/"}
    create_mock.assert_awaited_once()
    args, _ = create_mock.call_args
    assert args[1] == "/api/extras/journal-entries/"
    payload = args[2]
    assert payload["assigned_object_type"] == "virtualization.virtualmachine"
    assert payload["assigned_object_id"] == 42
    assert payload["kind"] == "info"
    assert payload["comments"] == "body"


async def test_write_verb_journal_entry_returns_none_on_failure():
    with patch(
        "proxbox_api.services.verb_dispatch.rest_create_async",
        new=AsyncMock(side_effect=RuntimeError("netbox down")),
    ):
        entry = await verb_dispatch.write_verb_journal_entry(
            nb=object(),
            netbox_vm_id=42,
            kind="warning",
            comments="body",
        )
    assert entry is None
