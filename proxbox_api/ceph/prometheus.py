"""Prometheus metric ingestion for Ceph v2.

Queries a Prometheus source for the current Ceph cluster state and normalizes
it into a bounded :class:`~proxbox_api.ceph.v2_schemas.CephMetricSnapshot`
(latest snapshot only — never a time series). The snapshot powers NetBox
health/capacity views and plan-time safety gating.

The client is ``httpx``-backed and accepts an injected ``httpx.AsyncClient`` so
tests can drive it with ``httpx.MockTransport`` and no live Prometheus.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from proxbox_api.ceph.v2_schemas import CephHealthStatus, CephMetricSnapshot

_DEFAULT_TIMEOUT = 15

# PromQL for each snapshot field. Instant queries against the ceph-mgr
# prometheus module's metric names. Missing/empty results normalize to None.
SNAPSHOT_QUERIES: dict[str, str] = {
    "health": "ceph_health_status",
    "osd_up": "sum(ceph_osd_up)",
    "osd_in": "sum(ceph_osd_in)",
    "osd_total": "count(ceph_osd_metadata)",
    "mon_quorum": "sum(ceph_mon_quorum_status)",
    "mgr_active": "sum(ceph_mgr_status)",
    "bytes_total": "ceph_cluster_total_bytes",
    "bytes_used": "ceph_cluster_total_used_bytes",
    "pgs_total": "ceph_pg_total",
    "degraded_pgs": "sum(ceph_pg_degraded)",
    "misplaced_pgs": "sum(ceph_pg_misplaced)",
    "recovering_pgs": "sum(ceph_pg_recovering)",
    "iops_read": "sum(rate(ceph_osd_op_r[5m]))",
    "iops_write": "sum(rate(ceph_osd_op_w[5m]))",
    "throughput_read_bps": "sum(rate(ceph_osd_op_r_out_bytes[5m]))",
    "throughput_write_bps": "sum(rate(ceph_osd_op_w_in_bytes[5m]))",
    "pools": "count(ceph_pool_metadata)",
}

_HEALTH_BY_CODE: dict[int, CephHealthStatus] = {
    0: "HEALTH_OK",
    1: "HEALTH_WARN",
    2: "HEALTH_ERR",
}


@dataclass(frozen=True, slots=True)
class PrometheusSourceConfig:
    """Connection config for a Prometheus source (secrets already decrypted)."""

    url: str
    bearer_token: str | None = None
    verify_ssl: bool = True
    timeout: int = _DEFAULT_TIMEOUT


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PrometheusClient:
    """Minimal async Prometheus HTTP API v1 client."""

    def __init__(
        self,
        config: PrometheusSourceConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._base_url = config.url.rstrip("/")
        self._owns_client = client is None
        headers = {}
        if config.bearer_token:
            headers["Authorization"] = f"Bearer {config.bearer_token}"
        self._client = client or httpx.AsyncClient(
            verify=config.verify_ssl,
            timeout=config.timeout,
            headers=headers,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> PrometheusClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def query(self, promql: str) -> list[dict[str, Any]]:
        """Run an instant query; return the raw result vector (may be empty)."""

        response = await self._client.get(
            f"{self._base_url}/api/v1/query", params={"query": promql}
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("status") != "success":
            return []
        data = body.get("data") or {}
        result = data.get("result") if isinstance(data, dict) else None
        return [r for r in result if isinstance(r, dict)] if isinstance(result, list) else []

    async def query_scalar(self, promql: str) -> float | None:
        """Run an instant query and reduce the result vector to one float."""

        vector = await self.query(promql)
        total: float | None = None
        for sample in vector:
            value = sample.get("value")
            if isinstance(value, list) and len(value) == 2:
                try:
                    total = (total or 0.0) + float(value[1])
                except (TypeError, ValueError):
                    continue
        return total


def _as_int(value: float | None) -> int | None:
    return int(value) if value is not None else None


async def collect_snapshot(
    client: PrometheusClient, *, source_url: str | None = None
) -> CephMetricSnapshot:
    """Query all snapshot metrics concurrently and normalize them."""

    keys = list(SNAPSHOT_QUERIES)
    warnings: list[str] = []

    async def _run(promql: str) -> float | None:
        try:
            return await client.query_scalar(promql)
        except httpx.HTTPError:
            # Transport exceptions can contain URLs, credentials, or response
            # fragments. Keep only the fixed query identifier at this boundary.
            warnings.append(f"query failed ({promql})")
            return None

    values = await asyncio.gather(*(_run(SNAPSHOT_QUERIES[k]) for k in keys))
    data = dict(zip(keys, values, strict=True))

    health_code = data.get("health")
    health: CephHealthStatus = "unknown"
    if health_code is not None:
        health = _HEALTH_BY_CODE.get(int(health_code), "unknown")

    bytes_total = _as_int(data.get("bytes_total"))
    bytes_used = _as_int(data.get("bytes_used"))
    bytes_avail = (
        bytes_total - bytes_used if bytes_total is not None and bytes_used is not None else None
    )
    percent_used = (
        round(bytes_used / bytes_total * 100, 2) if bytes_total and bytes_used is not None else None
    )

    pg_states: dict[str, int] = {}
    for state in ("degraded", "misplaced", "recovering"):
        count = _as_int(data.get(f"{state}_pgs"))
        if count:
            pg_states[state] = count

    return CephMetricSnapshot(
        cluster_health=health,
        captured_at=_utcnow(),
        source_url=source_url,
        bytes_total=bytes_total,
        bytes_used=bytes_used,
        bytes_avail=bytes_avail,
        percent_used=percent_used,
        osd_up=_as_int(data.get("osd_up")),
        osd_in=_as_int(data.get("osd_in")),
        osd_total=_as_int(data.get("osd_total")),
        mon_quorum=_as_int(data.get("mon_quorum")),
        mgr_active=_as_int(data.get("mgr_active")),
        pgs_total=_as_int(data.get("pgs_total")),
        pg_states=pg_states,
        degraded_pgs=_as_int(data.get("degraded_pgs")),
        misplaced_pgs=_as_int(data.get("misplaced_pgs")),
        recovering_pgs=_as_int(data.get("recovering_pgs")),
        iops_read=data.get("iops_read"),
        iops_write=data.get("iops_write"),
        throughput_read_bps=data.get("throughput_read_bps"),
        throughput_write_bps=data.get("throughput_write_bps"),
        pools=_as_int(data.get("pools")),
        warnings=warnings,
    )


async def fetch_snapshot(config: PrometheusSourceConfig) -> CephMetricSnapshot:
    """Construct a client from ``config`` and collect one snapshot."""

    async with PrometheusClient(config) as client:
        return await collect_snapshot(client, source_url=config.url)


async def validate_source(config: PrometheusSourceConfig) -> tuple[bool, str | None]:
    """Probe a Prometheus source. Returns ``(ok, error_message)``."""

    try:
        async with PrometheusClient(config) as client:
            await client.query("ceph_health_status")
        return True, None
    except httpx.HTTPError:
        return False, "Prometheus source validation failed."
