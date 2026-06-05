"""Prometheus Ceph v2 provider adapter.

Read/metrics-only provider: it ingests current Ceph metrics from a configured
Prometheus source and exposes them as a bounded snapshot. It does not perform
writes (diff/plan/apply/reconcile raise ``CephCapabilityUnsupported``).

The Prometheus source config is resolved by the route (which has DB access) and
injected via ``scope["prometheus_source"]``; it can also be passed directly to
the constructor for tests or external-cluster composition.
"""

from __future__ import annotations

from typing import Any

from proxbox_api.ceph.prometheus import PrometheusSourceConfig, fetch_snapshot
from proxbox_api.ceph.v2_providers.base import (
    CephCapabilityUnsupported,
    CephProviderAdapter,
)
from proxbox_api.ceph.v2_schemas import (
    DesiredStateBundle,
    ProviderCapabilities,
    ProviderOperation,
)


class PrometheusCephProviderAdapter(CephProviderAdapter):
    """Metrics/health provider backed by a Prometheus source (read-only)."""

    provider = "prometheus"

    def __init__(
        self,
        pxs: list[object] | None = None,  # noqa: ARG002 - registry-compatible signature
        *,
        source: PrometheusSourceConfig | None = None,
    ) -> None:
        self._source = source

    def _resolve_source(self, scope: dict[str, Any]) -> PrometheusSourceConfig | None:
        candidate = self._source or scope.get("prometheus_source")
        return candidate if isinstance(candidate, PrometheusSourceConfig) else None

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.provider,
            supported=True,
            read_state=True,
            metrics=True,
            diff=False,
            plan=False,
            apply=False,
            reconcile=False,
            destructive_operations=False,
            notes=["prometheus is a read-only metrics/health provider"],
        )

    async def metrics(self, scope: dict[str, Any]) -> dict[str, Any]:
        source = self._resolve_source(scope)
        if source is None:
            return {}
        snapshot = await fetch_snapshot(source)
        return snapshot.model_dump(mode="json")

    async def read_state(self, scope: dict[str, Any]) -> dict[str, Any]:
        source = self._resolve_source(scope)
        if source is None:
            return {}
        snapshot = await fetch_snapshot(source)
        return {
            "health": snapshot.cluster_health,
            "summary": {
                "cluster_health": snapshot.cluster_health,
                "percent_used": snapshot.percent_used,
                "osd_up": snapshot.osd_up,
                "osd_total": snapshot.osd_total,
            },
            "snapshot": snapshot.model_dump(mode="json"),
        }

    def _unsupported(self, capability: str) -> CephCapabilityUnsupported:
        return CephCapabilityUnsupported(
            f"prometheus provider is read-only; '{capability}' is not supported."
        )

    async def diff(
        self,
        desired: DesiredStateBundle,  # noqa: ARG002
        live: dict[str, Any],  # noqa: ARG002
    ) -> list[ProviderOperation]:
        raise self._unsupported("diff")

    async def plan(
        self,
        operations: list[ProviderOperation],  # noqa: ARG002
    ) -> list[ProviderOperation]:
        raise self._unsupported("plan")

    async def apply(
        self,
        operation: ProviderOperation,  # noqa: ARG002
        *,
        confirm_destructive: bool,  # noqa: ARG002
    ) -> dict[str, Any]:
        raise self._unsupported("apply")

    async def reconcile(self, scope: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise self._unsupported("reconcile")


__all__ = ["PrometheusCephProviderAdapter"]
