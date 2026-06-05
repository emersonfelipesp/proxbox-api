"""Prometheus provider adapter + plan-time metric safety gating (Ceph v2 #94)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from proxbox_api.ceph import v2_engine
from proxbox_api.ceph.prometheus import PrometheusSourceConfig
from proxbox_api.ceph.v2_engine import build_plan, metric_safety_validations
from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported
from proxbox_api.ceph.v2_providers.prometheus import PrometheusCephProviderAdapter
from proxbox_api.ceph.v2_schemas import (
    CephMetricSnapshot,
    PlanRequest,
    ProviderCapabilities,
    ProviderOperation,
)


def _snapshot(**kwargs: Any) -> CephMetricSnapshot:
    base = {"cluster_health": "HEALTH_OK", "captured_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    base.update(kwargs)
    return CephMetricSnapshot(**base)


async def test_adapter_capabilities_are_read_metrics_only() -> None:
    caps = await PrometheusCephProviderAdapter().capabilities()
    assert caps.supported is True
    assert caps.metrics is True and caps.read_state is True
    assert caps.apply is False and caps.diff is False and caps.plan is False


async def test_adapter_metrics_without_source_returns_empty() -> None:
    adapter = PrometheusCephProviderAdapter()
    assert await adapter.metrics({}) == {}


async def test_adapter_metrics_with_source_returns_snapshot(monkeypatch) -> None:
    snap = _snapshot(cluster_health="HEALTH_WARN", osd_up=5)

    async def fake_fetch(config: PrometheusSourceConfig) -> CephMetricSnapshot:
        assert config.url == "http://prom:9090"
        return snap

    monkeypatch.setattr("proxbox_api.ceph.v2_providers.prometheus.fetch_snapshot", fake_fetch)
    adapter = PrometheusCephProviderAdapter(source=PrometheusSourceConfig(url="http://prom:9090"))
    metrics = await adapter.metrics({})
    assert metrics["cluster_health"] == "HEALTH_WARN"
    assert metrics["osd_up"] == 5


async def test_adapter_write_paths_raise_unsupported() -> None:
    adapter = PrometheusCephProviderAdapter()
    with pytest.raises(CephCapabilityUnsupported):
        await adapter.apply(
            ProviderOperation(kind="pool", target_ref="rbd"), confirm_destructive=True
        )
    with pytest.raises(CephCapabilityUnsupported):
        await adapter.reconcile({})


def test_metric_safety_validations_only_warns_when_degraded() -> None:
    ops = [
        ProviderOperation(kind="pool", target_ref="rbd", action="delete", is_destructive=True),
        ProviderOperation(kind="pool", target_ref="rbd2", action="ensure"),
    ]
    # Healthy cluster -> no warnings.
    assert metric_safety_validations(_snapshot(), ops) == []
    # Degraded cluster -> one warning for the destructive op only.
    degraded = _snapshot(cluster_health="HEALTH_WARN", recovering_pgs=3)
    results = metric_safety_validations(degraded, ops)
    assert len(results) == 1
    assert results[0].severity == "warning"
    assert results[0].code == "cluster_degraded"
    assert results[0].target == "rbd"


class _FakeAdapter:
    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider="proxmox", supported=True, plan=False, apply=True)

    async def read_state(self, scope: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return {"summary": {}}

    async def diff(self, desired: Any, live: Any) -> list[Any]:  # noqa: ARG002
        return []

    async def plan(self, operations: list[Any]) -> list[Any]:
        return operations

    async def apply(self, operation: Any, *, confirm_destructive: bool) -> dict[str, Any]:
        return {}

    async def reconcile(self, scope: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return {}

    async def metrics(self, scope: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return {}


async def test_build_plan_consumes_metric_snapshot_for_warnings(monkeypatch) -> None:
    monkeypatch.setattr(v2_engine, "_PLAN_STORE", {})
    monkeypatch.setattr(v2_engine, "_PLAN_STORE_ORDER", [])
    request = PlanRequest.model_validate(
        {
            "provider": "proxmox",
            "operations": [{"kind": "pool", "target_ref": "rbd", "action": "delete"}],
        }
    )
    snapshot = _snapshot(cluster_health="HEALTH_ERR", degraded_pgs=10)
    plan = await build_plan(request, _FakeAdapter(), metric_snapshot=snapshot)
    warns = [v for v in plan.validations if v.code == "cluster_degraded"]
    assert warns, "degraded snapshot must surface a safety warning"
    # warnings (not errors) keep the plan valid
    assert plan.valid is True


async def test_build_plan_reads_snapshot_from_request_scope(monkeypatch) -> None:
    monkeypatch.setattr(v2_engine, "_PLAN_STORE", {})
    monkeypatch.setattr(v2_engine, "_PLAN_STORE_ORDER", [])
    snapshot = _snapshot(cluster_health="HEALTH_WARN", misplaced_pgs=5)
    request = PlanRequest.model_validate(
        {
            "provider": "proxmox",
            "operations": [{"kind": "pool", "target_ref": "rbd", "action": "delete"}],
            "scope": {"metric_snapshot": snapshot.model_dump(mode="json")},
        }
    )
    plan = await build_plan(request, _FakeAdapter())
    assert any(v.code == "cluster_degraded" for v in plan.validations)
