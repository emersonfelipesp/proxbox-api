"""Tests for the SyncContext ↔ X-NetBox-Branch bridge on the cluster route.

Issue #406 Phase 2. The cluster route at
``proxbox_api/routes/sync/individual/cluster.py`` accepts a
``netbox_branch_schema_id`` query parameter and must:

1. Populate :attr:`SyncContext.netbox_branch` with the schema id (or "" when
   absent). The field stays observational — reconcilers do not branch on
   it — but loggers and SSE emitters can read it from the context.
2. Wrap the call to ``sync_cluster_individual`` in
   ``netbox_session.activate_branch(schema_id)`` so every NetBox write
   carries ``X-NetBox-Branch``, mirroring the full-update gate added by
   issue #370.

These tests pin the bridge contract end-to-end without exercising any
real reconciler — ``sync_cluster_individual`` itself is stubbed.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator

from proxbox_api.services.run_session import SyncContext


class _RecordingSession:
    """Stand-in for the netbox-sdk Api facade.

    Captures the schema id seen at ``activate_branch`` entry and which
    SyncContext is observed at "request time" via ``record_call()``.
    """

    def __init__(self) -> None:
        self._active: str | None = None
        self.activated_with: list[str] = []
        self.calls: list[tuple[bool, str | None]] = []

    @contextmanager
    def activate_branch(self, schema_id: str) -> Iterator["_RecordingSession"]:
        self.activated_with.append(schema_id)
        prior = self._active
        self._active = schema_id
        try:
            yield self
        finally:
            self._active = prior

    def record_call(self) -> None:
        self.calls.append((self._active is not None, self._active))


def _make_settings():
    from proxbox_api.schemas import PluginConfig
    from proxbox_api.schemas.netbox import NetboxSessionSchema

    return PluginConfig(
        proxmox=[],
        netbox=NetboxSessionSchema(domain="netbox.example.com", http_port=8000, token="test-token"),
    )


def _stub_cluster_helpers(monkeypatch, captured_ctx: list[SyncContext]):
    """Patch ``resolve_proxmox_session`` and ``sync_cluster_individual``.

    The bridge's contract is "build SyncContext correctly and open the
    branch scope before calling sync_cluster_individual". We don't need
    to run the real reconciler to verify that — only to capture what the
    route hands it.
    """
    import proxbox_api.routes.sync.individual.cluster as route_module

    monkeypatch.setattr(
        route_module,
        "resolve_proxmox_session",
        lambda pxs, name: SimpleNamespace(cluster=SimpleNamespace(name=name)),
    )

    async def _stub_sync(ctx: SyncContext, cluster_name: str, *, dry_run: bool = False):
        captured_ctx.append(ctx)
        # Touch the recording session so the test can prove the branch
        # scope is open at "request time".
        if hasattr(ctx.nb, "record_call"):
            ctx.nb.record_call()
        return {"action": "unchanged", "cluster_name": cluster_name, "dry_run": dry_run}

    monkeypatch.setattr(route_module, "sync_cluster_individual", _stub_sync)


def test_cluster_route_threads_schema_id_into_sync_context(monkeypatch):
    """When the query param is set, ``ctx.netbox_branch`` carries it."""
    captured: list[SyncContext] = []
    _stub_cluster_helpers(monkeypatch, captured)

    from proxbox_api.routes.sync.individual.cluster import sync_cluster

    nb = _RecordingSession()
    asyncio.run(
        sync_cluster(
            nb=nb,
            pxs=[SimpleNamespace(cluster=SimpleNamespace(name="lab"))],
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            settings=_make_settings(),
            cluster_name="lab",
            dry_run=False,
            netbox_branch_schema_id="abcd1234",
        )
    )

    assert captured, "sync_cluster_individual was not invoked"
    assert captured[0].netbox_branch == "abcd1234"
    assert nb.activated_with == ["abcd1234"]
    # The request observed the branch as active.
    assert nb.calls == [(True, "abcd1234")]


def test_cluster_route_leaves_branch_unset_when_schema_id_omitted(monkeypatch):
    """No schema id ⇒ ``ctx.netbox_branch == ""`` and no activate_branch call."""
    captured: list[SyncContext] = []
    _stub_cluster_helpers(monkeypatch, captured)

    from proxbox_api.routes.sync.individual.cluster import sync_cluster

    nb = _RecordingSession()
    asyncio.run(
        sync_cluster(
            nb=nb,
            pxs=[SimpleNamespace(cluster=SimpleNamespace(name="lab"))],
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            settings=_make_settings(),
            cluster_name="lab",
            dry_run=False,
        )
    )

    assert captured[0].netbox_branch == ""
    assert nb.activated_with == []
    assert nb.calls == [(False, None)]


def test_cluster_route_treats_empty_schema_id_as_unset(monkeypatch):
    """An empty string from the query layer must not open a branch scope.

    FastAPI may surface ``?netbox_branch_schema_id=`` as ``""`` rather than
    ``None`` depending on parsing. The route uses ``or ""`` falsy-coalesce so
    both cases land in the no-branch path.
    """
    captured: list[SyncContext] = []
    _stub_cluster_helpers(monkeypatch, captured)

    from proxbox_api.routes.sync.individual.cluster import sync_cluster

    nb = _RecordingSession()
    asyncio.run(
        sync_cluster(
            nb=nb,
            pxs=[SimpleNamespace(cluster=SimpleNamespace(name="lab"))],
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            settings=_make_settings(),
            cluster_name="lab",
            dry_run=False,
            netbox_branch_schema_id="",
        )
    )

    assert captured[0].netbox_branch == ""
    assert nb.activated_with == []


def test_cluster_route_skips_branch_scope_when_resolve_fails(monkeypatch):
    """Early-return on resolve failure must not open a branch scope.

    ``resolve_proxmox_session`` returning ``None`` is a precondition
    failure — the route returns a structured error. No NetBox writes
    happen, so no branch header is needed.
    """
    import proxbox_api.routes.sync.individual.cluster as route_module

    monkeypatch.setattr(route_module, "resolve_proxmox_session", lambda pxs, name: None)

    async def _unreachable_sync(*args, **kwargs):  # pragma: no cover - asserted below
        raise AssertionError("sync_cluster_individual must not run when resolve fails")

    monkeypatch.setattr(route_module, "sync_cluster_individual", _unreachable_sync)

    nb = _RecordingSession()
    result = asyncio.run(
        route_module.sync_cluster(
            nb=nb,
            pxs=[],
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            settings=_make_settings(),
            cluster_name="ghost",
            dry_run=False,
            netbox_branch_schema_id="abcd1234",
        )
    )

    assert "error" in result
    assert nb.activated_with == []
