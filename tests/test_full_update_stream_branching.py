"""Tests that the /full-update/stream SSE entry point activates the netbox-sdk
branch context when ``netbox_branch_schema_id`` is supplied as a query parameter.

The stream wraps its event loop in ``netbox_session.activate_branch(schema_id)``
so the X-NetBox-Branch header is attached to every NetBox write performed
during the run. These tests pin that contract without needing a live FastAPI
client.

Port of PR #74 (`feat(#370): thread netbox_branch_schema_id ...`) onto
v0.0.11. v0.0.11's stream route takes ``request: Request`` as its first
positional argument and uses the ``_create_*`` private helpers for backups
and snapshots, so the stub list and call signature differ from the
``main``-branch original.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock


def _stub_dependencies(monkeypatch):
    """Patch all stage helpers so the stream completes quickly."""
    targets = [
        ("create_proxmox_devices", lambda **kw: asyncio.sleep(0, result=[])),
        ("create_virtual_machines", lambda **kw: asyncio.sleep(0, result=[])),
        ("create_storages", lambda **kw: asyncio.sleep(0, result=[])),
        (
            "create_virtual_disks",
            lambda **kw: asyncio.sleep(
                0, result={"count": 0, "created": 0, "updated": 0, "skipped": 0}
            ),
        ),
        ("create_all_virtual_machine_backups", lambda **kw: asyncio.sleep(0, result=[])),
        ("_create_all_virtual_machine_backups", lambda **kw: asyncio.sleep(0, result=[])),
        (
            "create_all_virtual_machine_snapshots",
            lambda **kw: asyncio.sleep(0, result={"count": 0, "created": 0, "skipped": 0}),
        ),
        (
            "_create_all_virtual_machine_snapshots",
            lambda **kw: asyncio.sleep(0, result={"count": 0, "created": 0, "skipped": 0}),
        ),
        (
            "sync_all_virtual_machine_task_histories",
            lambda **kw: asyncio.sleep(0, result={"count": 0, "created": 0, "skipped": 0}),
        ),
        ("create_all_device_interfaces", lambda **kw: asyncio.sleep(0, result=[])),
        ("create_only_vm_interfaces", lambda **kw: asyncio.sleep(0, result=[])),
        ("create_only_vm_ip_addresses", lambda **kw: asyncio.sleep(0, result=[])),
        (
            "sync_all_replications",
            lambda **kw: asyncio.sleep(0, result={"created": 0, "updated": 0}),
        ),
        (
            "sync_all_backup_routines",
            lambda **kw: asyncio.sleep(0, result={"created": 0, "updated": 0}),
        ),
    ]
    for name, fn in targets:
        monkeypatch.setattr(f"proxbox_api.app.full_update.{name}", fn)


def _fake_request() -> SimpleNamespace:
    """Mimic just enough of starlette.Request for the stream route."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(bootstrap_status=None)))


async def _drain_stream(response) -> None:
    """Consume the StreamingResponse body so the inner context manager exits."""
    async for _ in response.body_iterator:
        pass


def test_stream_does_not_activate_branch_when_schema_id_missing(monkeypatch):
    _stub_dependencies(monkeypatch)

    from proxbox_api.main import full_update_sync_stream

    session = SimpleNamespace(activate_branch=AsyncMock())

    response = asyncio.run(
        full_update_sync_stream(
            request=_fake_request(),
            netbox_session=session,
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=type("Tag", (), {"id": 1, "name": "Proxbox", "slug": "proxbox", "color": "ff0"})(),
        )
    )
    asyncio.run(_drain_stream(response))

    session.activate_branch.assert_not_called()


def test_stream_activates_branch_with_schema_id(monkeypatch):
    _stub_dependencies(monkeypatch)

    from proxbox_api.main import full_update_sync_stream

    captured: list[Any] = []
    entered = 0
    exited = 0

    @contextmanager
    def _activate(schema_id: Any) -> Iterator[None]:
        nonlocal entered, exited
        captured.append(schema_id)
        entered += 1
        try:
            yield
        finally:
            exited += 1

    session = SimpleNamespace(activate_branch=_activate)

    response = asyncio.run(
        full_update_sync_stream(
            request=_fake_request(),
            netbox_session=session,
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=type("Tag", (), {"id": 1, "name": "Proxbox", "slug": "proxbox", "color": "ff0"})(),
            netbox_branch_schema_id="cafef00d",
        )
    )
    asyncio.run(_drain_stream(response))

    assert captured == ["cafef00d"]
    assert entered == 1
    assert exited == 1, "branch context must close after the stream finishes"


def test_stream_does_not_activate_branch_when_schema_id_empty(monkeypatch):
    """Empty-string schema_id must be treated as no branch."""
    _stub_dependencies(monkeypatch)

    from proxbox_api.main import full_update_sync_stream

    session = SimpleNamespace(activate_branch=AsyncMock())

    response = asyncio.run(
        full_update_sync_stream(
            request=_fake_request(),
            netbox_session=session,
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=type("Tag", (), {"id": 1, "name": "Proxbox", "slug": "proxbox", "color": "ff0"})(),
            netbox_branch_schema_id="",
        )
    )
    asyncio.run(_drain_stream(response))

    session.activate_branch.assert_not_called()
