"""Tests for X-NetBox-Branch header propagation through netbox-sdk activate_branch().

The plugin → proxbox-api branch header pass-through is implemented by calling
``netbox_session.activate_branch(schema_id)`` on the netbox-sdk Api facade,
which opens a ``header_scope(X-NetBox-Branch=...)`` for the duration of the
context. These tests pin that the header is set when a schema id is provided
and absent when it is not.

Port of PR #74 onto v0.0.11. The sync route on v0.0.11 calls only the public
``create_*`` helpers from inside ``_full_update_sync_impl``, so the stub list
matches the ``main``-branch original. The route is now reachable via the thin
wrapper ``full_update_sync`` → ``_full_update_sync_run`` → ``_full_update_sync_impl``.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import AsyncMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Stand-in for the netbox-sdk Api facade.

    Captures the active branch schema_id while inside ``activate_branch()`` and
    records every (header_active, value) seen at "request time" via ``record_call()``.
    """

    def __init__(self) -> None:
        self._active: str | None = None
        self.calls: list[tuple[bool, str | None]] = []

    @contextmanager
    def activate_branch(self, schema_id: str) -> Iterator["_RecordingSession"]:
        prior = self._active
        self._active = schema_id
        try:
            yield self
        finally:
            self._active = prior

    def record_call(self) -> None:
        self.calls.append((self._active is not None, self._active))


# ---------------------------------------------------------------------------
# activate_branch() context behavior
# ---------------------------------------------------------------------------


def test_activate_branch_sets_value_inside_scope():
    session = _RecordingSession()
    session.record_call()
    with session.activate_branch("abcd1234"):
        session.record_call()
    session.record_call()

    assert session.calls == [
        (False, None),
        (True, "abcd1234"),
        (False, None),
    ]


def test_nested_activate_branch_restores_outer():
    session = _RecordingSession()
    with session.activate_branch("outer-id"):
        session.record_call()
        with session.activate_branch("inner-id"):
            session.record_call()
        session.record_call()

    assert session.calls == [
        (True, "outer-id"),
        (True, "inner-id"),
        (True, "outer-id"),
    ]


# ---------------------------------------------------------------------------
# full_update_sync route: schema_id gating
# ---------------------------------------------------------------------------


def _stub_full_update_dependencies(monkeypatch):
    """Patch all stage helpers on full_update so the route runs to completion."""
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
        (
            "create_all_virtual_machine_snapshots",
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


def test_full_update_sync_does_not_call_activate_branch_without_schema_id(monkeypatch):
    _stub_full_update_dependencies(monkeypatch)

    from proxbox_api.main import full_update_sync

    session = SimpleNamespace(activate_branch=AsyncMock())
    asyncio.run(
        full_update_sync(
            netbox_session=session,
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=type("Tag", (), {"id": 1, "name": "Proxbox", "slug": "proxbox", "color": "ff0"})(),
        )
    )

    session.activate_branch.assert_not_called()


def test_full_update_sync_invokes_activate_branch_with_schema_id(monkeypatch):
    _stub_full_update_dependencies(monkeypatch)

    from proxbox_api.main import full_update_sync

    captured: list[str] = []

    @contextmanager
    def _activate(schema_id: str):
        captured.append(schema_id)
        yield

    session = SimpleNamespace(activate_branch=_activate)
    asyncio.run(
        full_update_sync(
            netbox_session=session,
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=type("Tag", (), {"id": 1, "name": "Proxbox", "slug": "proxbox", "color": "ff0"})(),
            netbox_branch_schema_id="abcd1234",
        )
    )

    assert captured == ["abcd1234"]


# ---------------------------------------------------------------------------
# settings_client must NEVER attach a branch header
# ---------------------------------------------------------------------------


def test_settings_client_module_does_not_reference_activate_branch():
    """Settings are read from main; a branch header on settings fetches would
    cause per-branch cache pollution. Pin the absence of the call in source."""
    import pathlib

    settings_path = (
        pathlib.Path(__file__).resolve().parent.parent / "proxbox_api" / "settings_client.py"
    )
    text = settings_path.read_text(encoding="utf-8")
    assert "activate_branch" not in text
    assert "X-NetBox-Branch" not in text
