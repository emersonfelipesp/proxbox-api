"""Ceph Dashboard provider adapter tests (#98)."""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.ceph.dashboard_client import DashboardEndpointConfig
from proxbox_api.ceph.v2_providers import dashboard as dash_mod
from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported, CephWriteGateDenied
from proxbox_api.ceph.v2_providers.dashboard import DashboardCephProviderAdapter
from proxbox_api.ceph.v2_schemas import DesiredStateBundle, ProviderOperation

CONFIG = DashboardEndpointConfig(base_url="https://ceph:8443", username="admin", password="x")


class _FakeDashboardClient:
    """Fake DashboardCephClient returning canned inventory; records writes."""

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._fail = fail or set()
        self.closed = False

    async def health(self) -> dict[str, Any]:
        if "health" in self._fail:
            raise RuntimeError("health down")
        return {"status": "HEALTH_OK"}

    async def pools(self) -> list[dict[str, Any]]:
        return [{"pool_name": "rbd", "size": 3}, {"pool_name": "cephfs_data", "size": 2}]

    async def osds(self) -> list[dict[str, Any]]:
        return [{"osd": 0, "up": True}, {"osd": 1, "up": True}]

    async def hosts(self) -> list[dict[str, Any]]:
        return [{"hostname": "ceph-1"}]

    async def filesystems(self) -> list[dict[str, Any]]:
        return [{"name": "cephfs"}]

    async def rbd_images(self) -> list[dict[str, Any]]:
        return [{"name": "vm-100", "pool_name": "rbd", "size": 1024}]

    async def rgw_buckets(self) -> list[dict[str, Any]]:
        return [{"bucket": "backups", "owner": "alice"}]

    async def pool_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("pool_create", payload))
        return {"name": "task-pool-create"}

    async def pool_delete(self, name: str, *, confirm_destroy: bool) -> dict[str, Any]:
        self.calls.append(("pool_delete", (name, confirm_destroy)))
        return {}

    async def close(self) -> None:
        self.closed = True


def _adapter(client: _FakeDashboardClient, *, endpoint: DashboardEndpointConfig | None = CONFIG):
    return DashboardCephProviderAdapter(endpoint=endpoint, client_factory=lambda _c: client)


async def test_capabilities_inactive_without_sdk(monkeypatch) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: False)
    caps = await DashboardCephProviderAdapter().capabilities()
    assert caps.supported is True
    assert caps.apply is False and caps.read_state is False
    assert any("0.0.11" in n for n in caps.notes)


async def test_capabilities_active_with_sdk(monkeypatch) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: True)
    caps = await DashboardCephProviderAdapter().capabilities()
    assert caps.apply is False and caps.destructive_operations is False
    assert caps.read_state is True and caps.plan is True
    assert caps.operation_kinds.get("pool:create") is False
    assert caps.operation_kinds.get("rbd_image:create") is False
    assert caps.operation_kinds.get("rgw_bucket:create") is False  # via rgw_admin/external
    assert caps.operation_kinds.get("pool:noop") is True
    assert "noop" not in caps.operation_kinds
    assert any("durably bound" in note for note in caps.notes)


async def test_capabilities_and_apply_are_default_off_without_trusted_gateway(
    monkeypatch,
) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: True)
    monkeypatch.delenv("PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY", raising=False)
    client = _FakeDashboardClient()
    adapter = _adapter(client)

    caps = await adapter.capabilities()
    assert caps.apply is False
    assert caps.operation_kinds["pool:create"] is False
    with pytest.raises(CephWriteGateDenied, match="durable endpoint"):
        await adapter.apply(
            ProviderOperation(kind="pool", target_ref="rbd", action="create"),
            confirm_destructive=False,
        )
    assert client.calls == []


async def test_read_state_collects_inventory_and_closes(monkeypatch) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: True)
    client = _FakeDashboardClient()
    adapter = _adapter(client)
    state = await adapter.read_state({})
    kinds = {r["kind"] for r in state["resources"]}
    assert {"pool", "osd", "host", "filesystem", "rbd_image", "rgw_bucket"} <= kinds
    refs = {r["target_ref"] for r in state["resources"] if r["kind"] == "pool"}
    assert refs == {"rbd", "cephfs_data"}
    assert state["summary"]["health"] == "HEALTH_OK"
    assert client.closed is True


async def test_read_state_no_endpoint_reports_error() -> None:
    adapter = DashboardCephProviderAdapter(endpoint=None)
    state = await adapter.read_state({})
    assert state["resources"] == []
    assert any("no Ceph Dashboard endpoint" in e for e in state["errors"])


async def test_read_state_partial_failure_is_isolated(monkeypatch) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: True)
    client = _FakeDashboardClient(fail={"health"})
    adapter = _adapter(client)
    state = await adapter.read_state({})
    assert any("health" in e for e in state["errors"])
    # other collectors still produced resources
    assert state["resources"]


async def test_read_state_and_close_diagnostics_never_expose_url_or_exception(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: True)
    canary = "https://operator:dashboard-secret@ceph.invalid?token=dashboard-canary"
    client = _FakeDashboardClient(fail={"health"})

    async def fail_close() -> None:
        raise RuntimeError(canary)

    client.close = fail_close  # type: ignore[method-assign]
    config = DashboardEndpointConfig(base_url=canary, username="admin", password="x")
    state = await _adapter(client, endpoint=config).read_state({})
    serialized = repr(state)

    assert "dashboard-secret" not in serialized
    assert "dashboard-canary" not in serialized
    assert "RuntimeError" not in serialized
    assert "health down" not in serialized
    assert "dashboard-secret" not in caplog.text
    assert "dashboard-canary" not in caplog.text


async def test_diff_classifies_create_update_noop_delete(monkeypatch) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: True)
    adapter = _adapter(_FakeDashboardClient())
    live = {
        "resources": [
            {"kind": "pool", "target_ref": "rbd", "summary": {"size": 3}},
        ]
    }
    bundle = DesiredStateBundle.model_validate(
        {
            "objects": [
                {"kind": "pool", "target_ref": "rbd", "payload": {"size": 3}},  # noop
                {"kind": "pool", "target_ref": "rbd", "payload": {"size": 5}, "action": "ensure"},
                {"kind": "pool", "target_ref": "new", "payload": {"size": 2}},  # create
                {"kind": "pool", "target_ref": "old", "action": "delete"},  # delete
            ]
        }
    )
    ops = await adapter.diff(bundle, live)
    by_target = {(o.target_ref, o.action) for o in ops}
    assert ("rbd", "noop") in by_target
    assert ("new", "create") in by_target
    assert ("old", "delete") in by_target


async def test_apply_pool_create_stays_closed_without_durable_authority(monkeypatch) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: True)
    client = _FakeDashboardClient()
    adapter = _adapter(client)
    op = ProviderOperation(
        kind="pool", target_ref="rbd", action="create", after_summary={"pool_name": "rbd"}
    )
    with pytest.raises(CephWriteGateDenied, match="durable endpoint"):
        await adapter.apply(op, confirm_destructive=False)
    assert client.calls == []


async def test_apply_unknown_noop_is_rejected_before_client_creation(monkeypatch) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: True)
    client_created = False

    def client_factory(_config):
        nonlocal client_created
        client_created = True
        return _FakeDashboardClient()

    adapter = DashboardCephProviderAdapter(endpoint=CONFIG, client_factory=client_factory)
    op = ProviderOperation(kind="unknown", target_ref="target", action="noop")

    with pytest.raises(CephCapabilityUnsupported, match="unsupported operation"):
        await adapter.apply(op, confirm_destructive=False)

    assert client_created is False


async def test_apply_blocked_without_sdk(monkeypatch) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: False)
    adapter = _adapter(_FakeDashboardClient())
    op = ProviderOperation(kind="pool", target_ref="rbd", action="create")
    with pytest.raises(CephWriteGateDenied, match="durable endpoint"):
        await adapter.apply(op, confirm_destructive=False)


async def test_apply_blocked_without_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: True)
    adapter = DashboardCephProviderAdapter(endpoint=None)
    op = ProviderOperation(kind="pool", target_ref="rbd", action="create")
    with pytest.raises(CephWriteGateDenied, match="durable endpoint"):
        await adapter.apply(op, confirm_destructive=False)
