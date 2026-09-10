"""Focused contract tests for the authenticated InfluxDB v2 metrics route."""

from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest
from pydantic import ValidationError

from proxbox_api.routes.proxmox import metrics as metrics_route
from proxbox_api.schemas.influx import InfluxFilter, InfluxQueryRequest
from proxbox_api.services import influx as influx_service
from proxbox_api.services.influx import (
    InfluxQueryClient,
    InfluxQueryError,
    build_flux_query,
)


def _request(**overrides: object) -> InfluxQueryRequest:
    values: dict[str, object] = {
        "url": "https://influx.example.test/",
        "org": "ops",
        "bucket": "proxmox",
        "token": "secret-token-value",
        "measurement": "cpu",
    }
    values.update(overrides)
    return InfluxQueryRequest.model_validate(values)


def _csv_response() -> bytes:
    return (
        b"#datatype,string,long,dateTime:RFC3339,double,string,string,string\n"
        b"#group,false,false,false,false,false,false,false\n"
        b"#default,_result,,,,,,\n"
        b",result,table,_time,_value,_field,_measurement,host\n"
        b",_result,0,2026-09-09T00:00:00Z,42.5,usage,cpu,pve01\n"
    )


async def test_mock_transport_builds_safe_flux_and_normalizes_annotated_csv() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        seen["content_type"] = request.headers["content-type"]
        seen["flux"] = request.content.decode()
        return httpx.Response(
            200, headers={"content-type": "text/csv; charset=utf-8"}, content=_csv_response()
        )

    async with InfluxQueryClient(_request(), transport=httpx.MockTransport(handler)) as client:
        result = await client.query()

    assert seen["url"] == "https://influx.example.test/api/v2/query?org=ops&type=flux"
    assert seen["authorization"] == "Bearer secret-token-value"
    assert seen["content_type"] == "application/vnd.flux"
    assert 'from(bucket: "proxmox")' in str(seen["flux"])
    assert result.columns == [
        "result",
        "table",
        "_time",
        "_value",
        "_field",
        "_measurement",
        "host",
    ]
    assert result.rows == [
        {
            "result": "_result",
            "table": 0,
            "_time": "2026-09-09T00:00:00Z",
            "_value": 42.5,
            "_field": "usage",
            "_measurement": "cpu",
            "host": "pve01",
        }
    ]
    assert result.response_format == "annotated_csv"


async def test_mock_transport_normalizes_supported_influx_json() -> None:
    payload = {
        "results": [
            {
                "tables": [
                    {
                        "columns": ["_time", "_value"],
                        "records": [["2026-09-09T00:00:00Z", 7]],
                    }
                ]
            }
        ]
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await InfluxQueryClient(_request(), client=inner).query()
    await inner.aclose()

    assert result.columns == ["_time", "_value"]
    assert result.rows == [{"_time": "2026-09-09T00:00:00Z", "_value": 7}]
    assert result.response_format == "json"


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [{"tables": [{"columns": ["value"], "records": [["x" * 4097]]}]}]},
        {"results": [{"tables": [{"columns": ["value"], "records": [[[1]]]}]}]},
    ],
)
async def test_json_cells_are_bounded_and_flat(payload: dict[str, object]) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with InfluxQueryClient(_request(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InfluxQueryError) as caught:
            await client.query()

    assert caught.value.reason == "influx_invalid_response"


async def test_normalized_json_output_cannot_amplify_past_response_budget() -> None:
    columns = [f"column_{index}_" + ("x" * 100) for index in range(128)]
    payload = {
        "results": [
            {
                "tables": [
                    {
                        "columns": columns,
                        "records": [[index for index in range(128)] for _ in range(20)],
                    }
                ]
            }
        ]
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, json=payload)
        assert len(response.content) < 128 * 1024
        return response

    async with InfluxQueryClient(
        _request(max_response_bytes=128 * 1024), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(InfluxQueryError) as caught:
            await client.query()

    assert caught.value.reason == "influx_response_too_large"


async def test_deeply_nested_json_maps_to_invalid_response() -> None:
    body = (
        b"{"
        + b'"results":[{"tables":[{"columns":["value"],"records":['
        + b"[" * 1100
        + b"0"
        + b"]" * 1100
        + b"]}]}]}"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    async with InfluxQueryClient(_request(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InfluxQueryError) as caught:
            await client.query()

    assert caught.value.reason == "influx_invalid_response"


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "secret upstream detail", "results": []},
        {"results": [{"error": "secret upstream detail"}]},
        {
            "results": [
                {"tables": [{"columns": ["value"], "records": [[1]]}]},
                {"error": "secret upstream detail"},
            ]
        },
    ],
)
async def test_json_error_envelopes_fail_closed(payload: dict[str, object]) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with InfluxQueryClient(_request(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InfluxQueryError) as caught:
            await client.query()

    assert caught.value.reason == "influx_upstream_error"
    assert "secret upstream detail" not in str(caught.value)


async def test_json_error_after_truncation_threshold_fails_closed() -> None:
    payload = {
        "results": [
            {"tables": [{"columns": ["value"], "records": [[1], [2]]}]},
            {"error": "secret trailing error"},
        ]
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with InfluxQueryClient(
        _request(max_rows=1), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(InfluxQueryError) as caught:
            await client.query()

    assert caught.value.reason == "influx_upstream_error"
    assert "secret trailing error" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b"#datatype,string,string\n,error,reference\n,secret failure,12\n",
        _csv_response()
        + b"#datatype,string,string\n,error,reference\n,secret trailing failure,12\n",
    ],
)
async def test_csv_error_tables_fail_closed(body: bytes) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/csv"}, content=body)

    async with InfluxQueryClient(
        _request(max_rows=1), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(InfluxQueryError) as caught:
            await client.query()

    assert caught.value.reason == "influx_upstream_error"
    assert "secret" not in str(caught.value)


async def test_annotated_csv_keeps_multiple_tables_as_rows() -> None:
    body = (
        b"#datatype,string,long,dateTime:RFC3339,double\n"
        b"#group,false,false,false,false\n"
        b"#default,_result,,,\n"
        b",result,table,_time,_value\n"
        b",_result,0,2026-09-09T00:00:00Z,1\n"
        b"#datatype,string,long,dateTime:RFC3339,double\n"
        b"#group,false,false,false,false\n"
        b"#default,_result,,,\n"
        b",result,table,_time,_value\n"
        b",_result,1,2026-09-09T00:01:00Z,2\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/csv"}, content=body)

    async with InfluxQueryClient(_request(), transport=httpx.MockTransport(handler)) as client:
        result = await client.query()

    assert result.row_count == 2
    assert [row["table"] for row in result.rows] == [0, 1]


def test_field_filter_targets_field_name_and_value() -> None:
    flux = build_flux_query(
        _request(filters=[InfluxFilter(scope="field", key="usage", value="user")])
    )

    assert 'r["_field"] == "usage"' in flux
    assert 'r["_value"] == "user"' in flux


def test_flux_is_structured_and_escapes_literals() -> None:
    request = _request(
        measurement='cpu") |> drop(columns: ["secret"]) //',
        filters=[InfluxFilter(key="host", value='pve" |> drop()')],
        aggregation={"every": "5m", "function": "mean"},
    )

    flux = build_flux_query(request)

    assert "\n  |> drop" not in flux
    assert "aggregateWindow(every: 5m, fn: mean, createEmpty: false)" in flux
    assert "limit(n: 1001)" in flux


def test_flux_escapes_interpolation_in_every_string_literal() -> None:
    request = _request(
        bucket="bucket-${secret}",
        measurement="metric-${secret}",
        fields=["cpu-${secret}"],
        filters=[InfluxFilter(key="host", value="value-${secret}")],
    )

    flux = build_flux_query(request)

    assert "${secret}" not in flux.replace(r"\${secret}", "")
    assert flux.count(r"\${secret}") == 4


def test_flux_query_accepts_a_bounded_field_alias_set() -> None:
    request = _request(fields=["mem", "memused", "mem_used"])

    flux = build_flux_query(request)

    assert '(r["_field"] == "mem" or r["_field"] == "memused" or r["_field"] == "mem_used")' in flux


def test_field_alias_set_is_deduplicated_and_mutually_exclusive() -> None:
    assert _request(fields=["mem", "mem", "memused"]).fields == ["mem", "memused"]
    with pytest.raises(ValidationError):
        _request(field="mem", fields=["memused"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "ftp://influx.example.test"),
        ("url", "http://influx.example.test"),
        ("url", "https://user:password@influx.example.test"),
        ("start", "now() |> drop()"),
        ("max_rows", 5001),
        ("max_response_bytes", 8 * 1024 * 1024 + 1),
    ],
)
def test_request_bounds_reject_unsafe_or_unbounded_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _request(**{field: value})


async def test_execute_rejects_unapproved_target_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(influx_service, "validate_endpoint_url", lambda _url: (False, "blocked"))

    with pytest.raises(InfluxQueryError) as caught:
        await influx_service.execute_influx_query(_request())

    assert caught.value.reason == "influx_target_not_allowed"
    assert caught.value.status_code == 400


def test_resolved_target_pins_a_safe_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(influx_service, "validate_endpoint_url", lambda _url: (True, "OK"))
    monkeypatch.setattr(
        influx_service.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    assert influx_service._resolved_target("https://influx.example.test") == "93.184.216.34"


def test_resolved_target_rejects_loopback_even_if_policy_allows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(influx_service, "validate_endpoint_url", lambda _url: (True, "OK"))
    monkeypatch.setattr(
        influx_service.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )

    with pytest.raises(InfluxQueryError) as caught:
        influx_service._resolved_target("https://influx.example.test")

    assert caught.value.reason == "influx_target_not_allowed"


async def test_upstream_error_is_secret_safe_and_bounded() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="token=secret-token-value")

    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(InfluxQueryError) as caught:
        await InfluxQueryClient(_request(), client=inner).query()
    await inner.aclose()

    assert caught.value.reason == "influx_upstream_error"
    assert "secret-token-value" not in str(caught.value)


async def test_response_size_bound_maps_to_secret_safe_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/csv"}, content=b"x" * 1025)

    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(InfluxQueryError) as caught:
        await InfluxQueryClient(_request(max_response_bytes=1024), client=inner).query()
    await inner.aclose()

    assert caught.value.reason == "influx_response_too_large"


async def test_compressed_response_is_rejected_before_decoding() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept_encoding"] = request.headers["accept-encoding"]
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=gzip.compress(b"compressed-provider-body"),
        )

    async with InfluxQueryClient(_request(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InfluxQueryError) as caught:
            await client.query()

    assert caught.value.reason == "influx_invalid_response"
    assert seen["accept_encoding"] == "identity"


async def test_timeout_transport_maps_to_stable_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timeout", request=request)

    async with InfluxQueryClient(_request(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InfluxQueryError) as caught:
            await client.query()

    assert caught.value.reason == "influx_timeout"
    assert caught.value.status_code == 504


async def test_execute_timeout_bounds_the_complete_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(influx_service, "_resolved_target", lambda _url: "93.184.216.34")

    async def slow_query(_client: InfluxQueryClient) -> object:
        await asyncio.sleep(2)
        raise AssertionError("deadline did not cancel the query")

    monkeypatch.setattr(InfluxQueryClient, "query", slow_query)

    with pytest.raises(InfluxQueryError) as caught:
        await influx_service.execute_influx_query(_request(timeout_seconds=1))

    assert caught.value.reason == "influx_timeout"
    assert caught.value.status_code == 504


async def test_route_is_authenticated_and_maps_failures_without_body_leak(
    auth_test_client, test_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = test_client.post("/proxmox/metrics/influx/query", json={})
    assert response.status_code == 401

    async def fail(_payload: InfluxQueryRequest) -> object:
        raise InfluxQueryError("influx_upstream_error")

    monkeypatch.setattr(metrics_route, "execute_influx_query", fail)
    response = auth_test_client.post(
        "/proxmox/metrics/influx/query",
        json={
            "url": "https://influx.example.test",
            "org": "ops",
            "bucket": "proxmox",
            "token": "secret-token-value",
            "measurement": "cpu",
        },
    )
    assert response.status_code == 502
    assert response.json() == {
        "detail": {
            "reason": "influx_upstream_error",
            "message": "InfluxDB rejected the query.",
        }
    }
    assert "secret-token-value" not in response.text
