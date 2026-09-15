"""External (non-Proxmox) Ceph cluster provider adapter tests (#97)."""

from __future__ import annotations

import importlib

import pytest

from proxbox_api.ceph.dashboard_client import DashboardEndpointConfig
from proxbox_api.ceph.prometheus import PrometheusSourceConfig
from proxbox_api.ceph.rgw_client import RGWAdminConfig
from proxbox_api.ceph.v2_providers import dashboard as dash_mod
from proxbox_api.ceph.v2_providers import external as ext_mod
from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported, CephWriteGateDenied
from proxbox_api.ceph.v2_providers.external import ExternalCephProviderAdapter
from proxbox_api.ceph.v2_schemas import ProviderOperation

DASH = DashboardEndpointConfig(base_url="https://ceph:8443", username="a", password="b")
PROM = PrometheusSourceConfig(url="http://prom:9090")
RGW = RGWAdminConfig(base_url="http://rgw:8080", access_key="AK", secret_key="SK")
prometheus_provider_module = importlib.import_module("proxbox_api.ceph.v2_providers.prometheus")


class _FakeDashboard:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def pools(self):
        return [{"pool_name": "rbd"}]

    async def osds(self):
        return []

    async def hosts(self):
        return []

    async def filesystems(self):
        return []

    async def rbd_images(self):
        return []

    async def rgw_buckets(self):
        return []

    async def health(self):
        return {"status": "HEALTH_OK"}

    async def pool_create(self, payload):
        self.calls.append("pool_create")
        return {"name": "task"}

    async def close(self):
        pass


class _FakeRGW:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def list_users(self):
        return [{"user_id": "alice"}]

    async def list_buckets(self):
        return ["backups"]

    async def create_user(self, uid, *, display_name):
        self.calls.append(("create_user", (uid,), {"display_name": display_name}))
        return {"user_id": uid}

    async def remove_user(self, uid, *, confirm_destroy):
        self.calls.append(("remove_user", (uid,), {"confirm_destroy": confirm_destroy}))
        return {}

    async def close(self):
        pass


def _enable_sdks(monkeypatch) -> None:
    monkeypatch.setattr(ext_mod, "dashboard_sdk_importable", lambda: True)
    monkeypatch.setattr(dash_mod, "dashboard_sdk_importable", lambda: True)


async def test_capabilities_report_configured_providers(monkeypatch) -> None:
    _enable_sdks(monkeypatch)
    adapter = ExternalCephProviderAdapter(
        dashboard=DASH,
        prometheus=PROM,
        rgw=RGW,
        ceph_version="18.2.4",
        dashboard_client_factory=lambda _c: _FakeDashboard(),
        rgw_client_factory=lambda _c: _FakeRGW(),
    )
    caps = await adapter.capabilities()
    assert caps.supported is True
    assert caps.apply is False and caps.destructive_operations is False
    assert caps.metrics is True
    assert caps.operation_kinds.get("pool:create") is False
    assert caps.operation_kinds.get("rgw_user:create") is False
    assert caps.operation_kinds.get("rgw_user:noop") is True
    assert "noop" not in caps.operation_kinds
    assert "18.2.4" in caps.notes[0]
    assert any("durably bound" in note for note in caps.notes)


async def test_capabilities_none_configured() -> None:
    caps = await ExternalCephProviderAdapter().capabilities()
    assert caps.supported is True
    assert caps.apply is False and caps.read_state is False
    assert "none" in caps.notes[0]


async def test_capabilities_and_apply_are_default_off_without_trusted_gateway(
    monkeypatch,
) -> None:
    _enable_sdks(monkeypatch)
    monkeypatch.delenv("PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY", raising=False)
    fake = _FakeDashboard()
    adapter = ExternalCephProviderAdapter(
        dashboard=DASH,
        dashboard_client_factory=lambda _c: fake,
    )

    caps = await adapter.capabilities()
    assert caps.apply is False
    assert caps.operation_kinds["pool:create"] is False
    with pytest.raises(CephWriteGateDenied, match="durable selector"):
        await adapter.apply(
            ProviderOperation(kind="pool", target_ref="rbd", action="create"),
            confirm_destructive=False,
        )
    assert fake.calls == []


async def test_read_state_merges_dashboard_and_rgw(monkeypatch) -> None:
    _enable_sdks(monkeypatch)
    adapter = ExternalCephProviderAdapter(
        dashboard=DASH,
        rgw=RGW,
        dashboard_client_factory=lambda _c: _FakeDashboard(),
        rgw_client_factory=lambda _c: _FakeRGW(),
    )
    state = await adapter.read_state({})
    kinds = {r["kind"] for r in state["resources"]}
    assert "pool" in kinds
    assert "rgw_user" in kinds and "rgw_bucket" in kinds
    assert state["summary"]["health"] == "HEALTH_OK"


async def test_rgw_read_and_close_diagnostics_are_secret_free(monkeypatch, caplog) -> None:
    _enable_sdks(monkeypatch)
    canary = "https://operator:rgw-secret@rgw.invalid?token=rgw-canary"

    class _FailingRGW(_FakeRGW):
        async def list_users(self):
            raise RuntimeError(canary)

        async def list_buckets(self):
            raise RuntimeError(canary)

        async def close(self):
            raise RuntimeError(canary)

    adapter = ExternalCephProviderAdapter(
        rgw=RGW,
        rgw_client_factory=lambda _c: _FailingRGW(),
    )
    state = await adapter.read_state({})
    serialized = repr(state)

    assert "rgw-secret" not in serialized
    assert "rgw-canary" not in serialized
    assert "RuntimeError" not in serialized
    assert "rgw-secret" not in caplog.text
    assert "rgw-canary" not in caplog.text


async def test_apply_pool_stays_closed_without_durable_authority(monkeypatch) -> None:
    _enable_sdks(monkeypatch)
    fake = _FakeDashboard()
    adapter = ExternalCephProviderAdapter(dashboard=DASH, dashboard_client_factory=lambda _c: fake)
    op = ProviderOperation(
        kind="pool", target_ref="rbd", action="create", after_summary={"pool_name": "rbd"}
    )
    with pytest.raises(CephWriteGateDenied, match="durable selector"):
        await adapter.apply(op, confirm_destructive=False)
    assert fake.calls == []


async def test_apply_rgw_user_stays_closed_without_durable_authority(monkeypatch) -> None:
    _enable_sdks(monkeypatch)
    fake = _FakeRGW()
    adapter = ExternalCephProviderAdapter(rgw=RGW, rgw_client_factory=lambda _c: fake)
    op = ProviderOperation(
        kind="rgw_user",
        target_ref="alice",
        action="create",
        after_summary={"display_name": "Alice"},
    )
    with pytest.raises(CephWriteGateDenied, match="durable selector"):
        await adapter.apply(op, confirm_destructive=False)
    assert fake.calls == []


async def test_apply_rgw_delete_requires_confirmation(monkeypatch) -> None:
    _enable_sdks(monkeypatch)
    adapter = ExternalCephProviderAdapter(rgw=RGW, rgw_client_factory=lambda _c: _FakeRGW())
    op = ProviderOperation(kind="rgw_user", target_ref="alice", action="delete")
    with pytest.raises(CephWriteGateDenied, match="durable selector"):
        await adapter.apply(op, confirm_destructive=False)


async def test_apply_pool_without_dashboard_is_unsupported() -> None:
    adapter = ExternalCephProviderAdapter(rgw=RGW, rgw_client_factory=lambda _c: _FakeRGW())
    op = ProviderOperation(kind="pool", target_ref="rbd", action="create")
    with pytest.raises(CephWriteGateDenied, match="durable selector"):
        await adapter.apply(op, confirm_destructive=False)


async def test_apply_unknown_kind_unsupported(monkeypatch) -> None:
    _enable_sdks(monkeypatch)
    adapter = ExternalCephProviderAdapter(
        dashboard=DASH, dashboard_client_factory=lambda _c: _FakeDashboard()
    )
    op = ProviderOperation(kind="crush_rule", target_ref="r1", action="create")
    with pytest.raises(CephCapabilityUnsupported):
        await adapter.apply(op, confirm_destructive=False)


async def test_apply_unknown_noop_is_rejected() -> None:
    adapter = ExternalCephProviderAdapter()
    op = ProviderOperation(kind="unknown", target_ref="target", action="noop")

    with pytest.raises(CephCapabilityUnsupported, match="unsupported operation"):
        await adapter.apply(op, confirm_destructive=False)


async def test_metrics_prefers_prometheus(monkeypatch) -> None:
    snap_dict = {"cluster_health": "HEALTH_WARN", "captured_at": "2026-01-01T00:00:00Z"}

    async def fake_fetch(_config):
        from proxbox_api.ceph.v2_schemas import CephMetricSnapshot

        return CephMetricSnapshot.model_validate(snap_dict)

    monkeypatch.setattr(prometheus_provider_module, "fetch_snapshot", fake_fetch)
    adapter = ExternalCephProviderAdapter(prometheus=PROM)
    metrics = await adapter.metrics({})
    assert metrics["cluster_health"] == "HEALTH_WARN"
