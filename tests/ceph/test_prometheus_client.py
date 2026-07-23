"""Tests for the Prometheus client + snapshot normalization (Ceph v2 #94)."""

from __future__ import annotations

import httpx
import pytest

from proxbox_api.ceph.prometheus import (
    PrometheusClient,
    PrometheusSourceConfig,
    collect_snapshot,
)


def _vector(value: float) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1700000000, str(value)]}],
        },
    }


def _empty() -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


# Canned values per PromQL query (keyed by a discriminating substring).
_RESPONSES = {
    "ceph_health_status": _vector(1),  # HEALTH_WARN
    "ceph_osd_up": _vector(5),
    "ceph_osd_in": _vector(5),
    "ceph_osd_metadata": _vector(6),
    "ceph_mon_quorum_status": _vector(3),
    "ceph_mgr_status": _vector(1),
    "ceph_cluster_total_bytes": _vector(1000),
    "ceph_cluster_total_used_bytes": _vector(250),
    "ceph_pg_total": _vector(128),
    "ceph_pg_degraded": _vector(4),
    "ceph_pg_misplaced": _empty(),
    "ceph_pg_recovering": _vector(2),
    "ceph_osd_op_r": _vector(10),
    "ceph_osd_op_w": _vector(20),
    "ceph_osd_op_r_out_bytes": _vector(1024),
    "ceph_osd_op_w_in_bytes": _vector(2048),
    "ceph_pool_metadata": _vector(3),
}


def _handler(request: httpx.Request) -> httpx.Response:
    query = request.url.params.get("query", "")
    # match the most specific key contained in the query
    for key in sorted(_RESPONSES, key=len, reverse=True):
        if key in query:
            return httpx.Response(200, json=_RESPONSES[key])
    return httpx.Response(200, json=_empty())


def _client() -> PrometheusClient:
    transport = httpx.MockTransport(_handler)
    inner = httpx.AsyncClient(transport=transport)
    return PrometheusClient(PrometheusSourceConfig(url="http://prom:9090"), client=inner)


async def test_query_and_query_scalar() -> None:
    client = _client()
    assert await client.query_scalar("ceph_pg_total") == 128.0
    assert await client.query_scalar("ceph_pg_misplaced") is None  # empty vector -> None
    await client.aclose()


async def test_collect_snapshot_normalizes_all_fields() -> None:
    client = _client()
    snap = await collect_snapshot(client, source_url="http://prom:9090")
    await client.aclose()
    assert snap.cluster_health == "HEALTH_WARN"
    assert snap.osd_up == 5 and snap.osd_in == 5 and snap.osd_total == 6
    assert snap.bytes_total == 1000 and snap.bytes_used == 250
    assert snap.bytes_avail == 750
    assert snap.percent_used == 25.0
    assert snap.pgs_total == 128
    assert snap.degraded_pgs == 4 and snap.recovering_pgs == 2
    assert snap.misplaced_pgs is None
    assert snap.pg_states == {"degraded": 4, "recovering": 2}
    assert snap.pools == 3
    assert snap.source_url == "http://prom:9090"
    assert snap.is_degraded is True  # WARN + recovery in flight


async def test_collect_snapshot_handles_no_data() -> None:
    def empty_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_empty())

    inner = httpx.AsyncClient(transport=httpx.MockTransport(empty_handler))
    client = PrometheusClient(PrometheusSourceConfig(url="http://prom:9090"), client=inner)
    snap = await collect_snapshot(client)
    await client.aclose()
    assert snap.cluster_health == "unknown"
    assert snap.osd_up is None and snap.bytes_total is None
    assert snap.pg_states == {}
    assert snap.is_degraded is False


async def test_collect_snapshot_records_query_errors_as_warnings() -> None:
    def err_handler(request: httpx.Request) -> httpx.Response:
        if "ceph_health_status" in request.url.params.get("query", ""):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_empty())

    inner = httpx.AsyncClient(transport=httpx.MockTransport(err_handler))
    client = PrometheusClient(PrometheusSourceConfig(url="http://prom:9090"), client=inner)
    snap = await collect_snapshot(client)
    await client.aclose()
    assert snap.cluster_health == "unknown"
    assert any("query failed" in w for w in snap.warnings)


async def test_collect_snapshot_never_exposes_transport_exception_text() -> None:
    secret_canary = "prometheus-transport-secret-canary"

    def err_handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise httpx.ConnectError(secret_canary)

    inner = httpx.AsyncClient(transport=httpx.MockTransport(err_handler))
    client = PrometheusClient(PrometheusSourceConfig(url="http://prom:9090"), client=inner)
    snap = await collect_snapshot(client)
    await client.aclose()

    serialized = snap.model_dump_json()
    assert secret_canary not in serialized
    assert snap.warnings
    assert all(warning.startswith("query failed (") for warning in snap.warnings)


async def test_bearer_token_sent_as_authorization_header() -> None:
    seen: dict[str, str] = {}

    def auth_handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=_empty())

    inner = httpx.AsyncClient(transport=httpx.MockTransport(auth_handler))
    config = PrometheusSourceConfig(url="http://prom:9090", bearer_token="s3cr3t")
    # bearer header is applied via the owned client; here we pass our own client,
    # so assert the config carries the token and the owned-client path builds it.
    client = PrometheusClient(config)
    assert client._client.headers.get("authorization") == "Bearer s3cr3t"
    await client.aclose()
    # the injected-client path doesn't re-inject; ensure no crash either way
    client2 = PrometheusClient(config, client=inner)
    await client2.query("ceph_health_status")
    await client2.aclose()


@pytest.mark.parametrize("code,expected", [(0, "HEALTH_OK"), (1, "HEALTH_WARN"), (2, "HEALTH_ERR")])
async def test_health_code_mapping(code: int, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "ceph_health_status" in request.url.params.get("query", ""):
            return httpx.Response(200, json=_vector(code))
        return httpx.Response(200, json=_empty())

    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PrometheusClient(PrometheusSourceConfig(url="http://prom:9090"), client=inner)
    snap = await collect_snapshot(client)
    await client.aclose()
    assert snap.cluster_health == expected
