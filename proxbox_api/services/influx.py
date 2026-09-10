"""Bounded, provider-neutral InfluxDB v2 query transport and normalization."""

from __future__ import annotations

import asyncio
import concurrent.futures
import csv
import ipaddress
import json
import os
import re
import socket
import ssl
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

from proxbox_api.schemas.influx import (
    InfluxFilter,
    InfluxQueryRequest,
    InfluxQueryResponse,
    InfluxQueryWindow,
    InfluxResponseFormat,
)
from proxbox_api.ssrf import validate_endpoint_url

MAX_INFLUX_COLUMNS = 128
MAX_INFLUX_CELL_LENGTH = 4096
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="proxbox-influx-dns"
)
_DURATION_PATTERN = re.compile(r"^-?(?:\d+(?:ns|us|µs|ms|s|m|h|d|w|mo|y))+$")
_PRIVATE_IPV4_RANGES = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

InfluxFailureReason = Literal[
    "influx_connection_error",
    "influx_empty_response",
    "influx_invalid_response",
    "influx_response_too_large",
    "influx_timeout",
    "influx_tls_error",
    "influx_upstream_error",
    "influx_target_not_allowed",
]


class InfluxQueryError(Exception):
    """Public-safe failure; it deliberately contains no URL, token, or body."""

    def __init__(self, reason: InfluxFailureReason, *, status_code: int = 502) -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


class _PinnedNetworkBackend(AnyIOBackend):
    """Connect to the IP validated for one request while preserving TLS SNI."""

    def __init__(self, address: str) -> None:
        super().__init__()
        self._address = address

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await super().connect_tcp(
            self._address, port, timeout, local_address, socket_options
        )


class _PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport that pins DNS resolution for the request lifetime."""

    def __init__(self, request: InfluxQueryRequest, address: str) -> None:
        ssl_context = httpx.create_ssl_context(
            verify=request.verify_ssl,
            trust_env=False,
        )
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=10,
            max_keepalive_connections=10,
            keepalive_expiry=5.0,
            network_backend=_PinnedNetworkBackend(address),
        )


def _resolved_addresses(host: str, port: int) -> set[str]:
    try:
        addresses = {
            str(ipaddress.ip_address(info[4][0]))
            for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            if info[4]
        }
    except (OSError, ValueError):
        raise InfluxQueryError("influx_target_not_allowed", status_code=400) from None
    if not addresses:
        raise InfluxQueryError("influx_target_not_allowed", status_code=400)

    return addresses


def _is_allowed_target_address(address: str) -> bool:
    parsed_address = ipaddress.ip_address(address)
    return parsed_address.is_global or any(
        parsed_address in network for network in _PRIVATE_IPV4_RANGES
    )


def _resolved_target(url: str) -> str:
    """Validate and return one safe address, preventing DNS rebinding on connect."""
    is_safe, _reason = validate_endpoint_url(url)
    if not is_safe:
        raise InfluxQueryError("influx_target_not_allowed", status_code=400)
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise InfluxQueryError("influx_target_not_allowed", status_code=400)
    try:
        port = parsed.port or 443
    except ValueError:
        raise InfluxQueryError("influx_target_not_allowed", status_code=400) from None
    addresses = _resolved_addresses(host, port)
    for address in sorted(addresses):
        if _is_allowed_target_address(address):
            return address
    raise InfluxQueryError("influx_target_not_allowed", status_code=400)


def _flux_string(value: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False)
    return encoded.replace("${", r"\${")


def _flux_time(value: str | None, *, default_now: bool = False) -> str:
    if value is None and default_now:
        return "now()"
    assert value is not None
    if _DURATION_PATTERN.fullmatch(value):
        return value
    return f"time(v: {_flux_string(value)})"


def _filter_expression(predicate: InfluxFilter) -> str:
    if predicate.scope == "field":
        return (
            f'r["_field"] == {_flux_string(predicate.key)} and '
            f'r["_value"] {predicate.operator} {_flux_string(predicate.value)}'
        )
    return f"r[{_flux_string(predicate.key)}] {predicate.operator} {_flux_string(predicate.value)}"


def build_flux_query(request: InfluxQueryRequest) -> str:
    """Build Flux exclusively from validated structured request fields."""
    predicates = [
        f'r["_measurement"] == {_flux_string(request.measurement)}',
    ]
    if request.field is not None:
        predicates.append(f'r["_field"] == {_flux_string(request.field)}')
    if request.fields:
        field_predicates = " or ".join(
            f'r["_field"] == {_flux_string(field)}' for field in request.fields
        )
        predicates.append(f"({field_predicates})")
    predicates.extend(_filter_expression(item) for item in request.filters)
    filter_clause = " and ".join(predicates)

    query = [
        f"from(bucket: {_flux_string(request.bucket)})",
        f"  |> range(start: {_flux_time(request.start)}, stop: {_flux_time(request.stop, default_now=True)})",
        f"  |> filter(fn: (r) => {filter_clause})",
    ]
    if request.aggregation is not None:
        query.append(
            "  |> aggregateWindow("
            f"every: {request.aggregation.every}, "
            f"fn: {request.aggregation.function}, createEmpty: false)"
        )
    query.append(f"  |> limit(n: {request.max_rows + 1})")
    return "\n".join(query)


def _normalize_cell(value: str, datatype: str | None) -> object:
    if value == "":
        return None
    if datatype in {"long", "unsignedLong"}:
        try:
            return int(value)
        except ValueError:
            return value
    if datatype == "double":
        try:
            return float(value)
        except ValueError:
            return value
    if datatype == "boolean":
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
    return value


def _bounded_columns(columns: Iterable[object]) -> list[str]:
    normalized = [str(column) for column in columns]
    if not normalized or len(normalized) > MAX_INFLUX_COLUMNS:
        raise InfluxQueryError("influx_invalid_response")
    if any(not column or len(column) > MAX_INFLUX_CELL_LENGTH for column in normalized):
        raise InfluxQueryError("influx_invalid_response")
    if len(set(normalized)) != len(normalized):
        raise InfluxQueryError("influx_invalid_response")
    return normalized


def _finalize_rows(
    columns: list[str],
    rows: list[dict[str, object]],
    request: InfluxQueryRequest,
    response_format: InfluxResponseFormat,
) -> InfluxQueryResponse:
    truncated = len(rows) > request.max_rows
    bounded_rows = rows[: request.max_rows]
    response = InfluxQueryResponse(
        columns=columns,
        rows=bounded_rows,
        row_count=len(bounded_rows),
        truncated=truncated,
        query_window=InfluxQueryWindow(start=request.start, stop=request.stop),
        captured_at=datetime.now(UTC),
        response_format=response_format,
    )
    _enforce_normalized_response_budget(response, request.max_response_bytes)
    return response


def _enforce_normalized_response_budget(response: InfluxQueryResponse, maximum_bytes: int) -> None:
    """Reject normalized output before HTTP serialization can amplify it."""
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    total = 0
    for chunk in encoder.iterencode(response.model_dump(mode="json")):
        total += len(chunk.encode("utf-8"))
        if total > maximum_bytes:
            raise InfluxQueryError("influx_response_too_large")


def _csv_metadata(lines: list[str]) -> tuple[list[str], list[str]]:
    datatypes = next((line.split(",")[1:] for line in lines if line.startswith("#datatype,")), [])
    defaults = next((line.split(",")[1:] for line in lines if line.startswith("#default,")), [])
    return datatypes, defaults


def _csv_row(
    raw_row: list[str],
    columns: list[str],
    datatypes: list[str],
    defaults: list[str],
) -> dict[str, object]:
    if len(raw_row) > len(columns) and raw_row[0] == "":
        raw_row = raw_row[1:]
    if len(raw_row) != len(columns):
        raise InfluxQueryError("influx_invalid_response")
    row: dict[str, object] = {}
    for index, column in enumerate(columns):
        value = raw_row[index]
        if value == "" and index < len(defaults) and defaults[index]:
            value = defaults[index]
        if len(value) > MAX_INFLUX_CELL_LENGTH:
            raise InfluxQueryError("influx_invalid_response")
        datatype = datatypes[index] if index < len(datatypes) else None
        row[column] = _normalize_cell(value, datatype)
    return row


def _csv_sections(lines: list[str]) -> list[list[str]]:
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("#datatype,") and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)
    return sections


def _normalize_csv_table(
    lines: list[str], max_rows: int
) -> tuple[list[str], list[dict[str, object]]]:
    datatypes, defaults = _csv_metadata(lines)
    data_lines = [line for line in lines if not line.startswith("#")]
    reader = csv.reader(data_lines)
    header = next((row for row in reader if row), None)
    if header is None:
        return [], []
    if header[0] == "":
        header = header[1:]
    columns = _bounded_columns(header)
    rows: list[dict[str, object]] = []
    for raw_row in reader:
        if not raw_row:
            continue
        rows.append(_csv_row(raw_row, columns, datatypes, defaults))
        if len(rows) >= max_rows:
            break
    return columns, rows


def _normalize_csv(body: bytes, request: InfluxQueryRequest) -> InfluxQueryResponse:
    try:
        text = body.decode("utf-8-sig")
        sections = _csv_sections(text.splitlines())
        _reject_csv_error_sections(sections)
        columns: list[str] = []
        rows: list[dict[str, object]] = []
        for section in sections:
            table_columns, table_rows = _normalize_csv_table(
                section, request.max_rows + 1 - len(rows)
            )
            for column in table_columns:
                if column not in columns:
                    columns.append(column)
            rows.extend(table_rows)
            if len(rows) > request.max_rows:
                break
        if not columns:
            raise InfluxQueryError("influx_empty_response")
        return _finalize_rows(_bounded_columns(columns), rows, request, "annotated_csv")
    except InfluxQueryError:
        raise
    except (csv.Error, UnicodeDecodeError, ValueError, IndexError) as exc:
        raise InfluxQueryError("influx_invalid_response") from exc


def _reject_csv_error_sections(sections: list[list[str]]) -> None:
    """Reject every Influx error table before metric-row truncation."""
    for section in sections:
        data_lines = [line for line in section if not line.startswith("#")]
        header = next((row for row in csv.reader(data_lines) if row), None)
        if header is None:
            continue
        if header[0] == "":
            header = header[1:]
        if "error" in header:
            raise InfluxQueryError("influx_upstream_error")


def _table_columns(raw_columns: object) -> list[str]:
    if not isinstance(raw_columns, list):
        raise InfluxQueryError("influx_invalid_response")
    if raw_columns and isinstance(raw_columns[0], dict):
        names: list[str] = []
        for item in raw_columns:
            if not isinstance(item, dict):
                raise InfluxQueryError("influx_invalid_response")
            item_data = cast(dict[str, object], item)
            name = item_data.get("name")
            if not isinstance(name, str):
                raise InfluxQueryError("influx_invalid_response")
            names.append(name)
        return _bounded_columns(names)
    return _bounded_columns(raw_columns)


def _bounded_json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and len(value) <= MAX_INFLUX_CELL_LENGTH:
        return value
    raise InfluxQueryError("influx_invalid_response")


def _json_row(raw_row: object, columns: list[str]) -> dict[str, object]:
    if isinstance(raw_row, dict):
        row_data = cast(dict[str, object], raw_row)
        return {column: _bounded_json_value(row_data.get(column)) for column in columns}
    if isinstance(raw_row, list) and len(raw_row) == len(columns):
        return {
            column: _bounded_json_value(value)
            for column, value in zip(columns, raw_row, strict=True)
        }
    raise InfluxQueryError("influx_invalid_response")


def _merge_json_columns(columns: list[str], table_columns: list[str]) -> None:
    for column in table_columns:
        if column not in columns:
            columns.append(column)


def _append_json_rows(
    columns: list[str],
    rows: list[dict[str, object]],
    raw_columns: list[str],
    raw_rows: object,
    row_limit: int,
) -> None:
    if not isinstance(raw_rows, list):
        raise InfluxQueryError("influx_invalid_response")
    for raw_row in raw_rows:
        if len(rows) >= row_limit:
            return
        rows.append(_json_row(raw_row, raw_columns))
        _merge_json_columns(columns, raw_columns)


def _json_tables(result: object) -> list[dict[str, object]]:
    if not isinstance(result, dict):
        raise InfluxQueryError("influx_invalid_response")
    result_data = cast(dict[str, object], result)
    if result_data.get("error") not in (None, ""):
        raise InfluxQueryError("influx_upstream_error")
    tables: list[dict[str, object]] = []
    for key in ("tables", "series"):
        value = result_data.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise InfluxQueryError("influx_invalid_response")
        tables.extend(cast(list[dict[str, object]], value))
    return tables


def _append_json_table(
    table: dict[str, object],
    columns: list[str],
    rows: list[dict[str, object]],
    row_limit: int,
) -> None:
    table_columns = _table_columns(table.get("columns"))
    raw_rows = table.get("records", table.get("values", []))
    _append_json_rows(columns, rows, table_columns, raw_rows, row_limit)


def _json_results(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        raise InfluxQueryError("influx_invalid_response")
    payload_data = cast(dict[str, object], payload)
    if payload_data.get("error") not in (None, ""):
        raise InfluxQueryError("influx_upstream_error")
    results = payload_data.get("results")
    if not isinstance(results, list):
        raise InfluxQueryError("influx_invalid_response")
    for result in results:
        _json_tables(result)
    return cast(list[object], results)


def _collect_json_rows(
    results: list[object], row_limit: int
) -> tuple[list[str], list[dict[str, object]]]:
    columns: list[str] = []
    rows: list[dict[str, object]] = []
    for result in results:
        for table in _json_tables(result):
            _append_json_table(table, columns, rows, row_limit)
            if len(rows) > row_limit - 1:
                return columns, rows
    return columns, rows


def _normalize_json(body: bytes, request: InfluxQueryRequest) -> InfluxQueryResponse:
    try:
        payload = json.loads(body)
        columns, rows = _collect_json_rows(_json_results(payload), request.max_rows + 1)
        if not columns and not rows:
            return _finalize_rows([], [], request, "json")
        if len(rows) > request.max_rows:
            rows = rows[: request.max_rows + 1]
        return _finalize_rows(_bounded_columns(columns), rows, request, "json")
    except InfluxQueryError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError, KeyError) as exc:
        raise InfluxQueryError("influx_invalid_response") from exc


def normalize_influx_response(
    body: bytes, content_type: str, request: InfluxQueryRequest
) -> InfluxQueryResponse:
    """Normalize one bounded annotated CSV or supported JSON response."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in {"text/csv", "application/csv"}:
        return _normalize_csv(body, request)
    if media_type == "application/json":
        return _normalize_json(body, request)
    stripped = body.lstrip()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        return _normalize_json(body, request)
    return _normalize_csv(body, request)


def _failure_for_transport(error: Exception) -> InfluxQueryError:
    if isinstance(error, httpx.TimeoutException):
        return InfluxQueryError("influx_timeout", status_code=504)
    cause = error.__cause__
    if isinstance(cause, ssl.SSLError) or "ssl" in str(error).lower():
        return InfluxQueryError("influx_tls_error")
    return InfluxQueryError("influx_connection_error")


class InfluxQueryClient:
    """Async InfluxDB v2 client with injectable HTTP client/transport."""

    def __init__(
        self,
        request: InfluxQueryRequest,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        pinned_address: str | None = None,
    ) -> None:
        self.request = request
        self._owns_client = client is None
        if transport is None and client is None and pinned_address is not None:
            transport = _PinnedHTTPTransport(request, pinned_address)
        self._client = client or httpx.AsyncClient(
            transport=transport,
            verify=request.verify_ssl,
            timeout=request.timeout_seconds,
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> InfluxQueryClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def query(self) -> InfluxQueryResponse:
        """Execute one structured query and normalize its bounded response."""
        url = _query_url(str(self.request.url))
        headers = {
            "Authorization": f"Bearer {self.request.token.get_secret_value()}",
            "Accept": "text/csv, application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/vnd.flux",
        }
        try:
            async with self._client.stream(
                "POST",
                url,
                params={"org": self.request.org, "type": "flux"},
                headers=headers,
                content=build_flux_query(self.request).encode("utf-8"),
            ) as response:
                if response.status_code >= 400:
                    raise InfluxQueryError("influx_upstream_error")
                content_encoding = response.headers.get("content-encoding", "identity").lower()
                if content_encoding not in {"", "identity"}:
                    raise InfluxQueryError("influx_invalid_response")
                body = await _read_bounded_body(response, self.request.max_response_bytes)
                if not body.strip():
                    raise InfluxQueryError("influx_empty_response")
                return normalize_influx_response(
                    body, response.headers.get("content-type", ""), self.request
                )
        except InfluxQueryError:
            raise
        except httpx.TransportError as exc:
            raise _failure_for_transport(exc) from exc


def _query_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/v2/query"):
        query_path = path
    elif path.endswith("/api/v2"):
        query_path = f"{path}/query"
    else:
        query_path = f"{path}/api/v2/query"
    return urlunsplit((parsed.scheme, parsed.netloc, query_path, "", ""))


async def _read_bounded_body(response: httpx.Response, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise InfluxQueryError("influx_response_too_large")
        except ValueError:
            pass
    if response.is_stream_consumed:
        if len(response.content) > max_bytes:
            raise InfluxQueryError("influx_response_too_large")
        return response.content
    body = bytearray()
    async for chunk in response.aiter_raw():
        if len(body) + len(chunk) > max_bytes:
            raise InfluxQueryError("influx_response_too_large")
        body.extend(chunk)
    return bytes(body)


async def execute_influx_query(request: InfluxQueryRequest) -> InfluxQueryResponse:
    """Execute a request with an owned client and close it deterministically."""
    try:
        async with asyncio.timeout(request.timeout_seconds):
            loop = asyncio.get_running_loop()
            pinned_address = await loop.run_in_executor(
                _DNS_EXECUTOR, _resolved_target, str(request.url)
            )
            if not request.verify_ssl and os.environ.get(
                "PROXBOX_ALLOW_INSECURE_INFLUX_TLS", ""
            ).lower() not in {"1", "true", "yes", "on"}:
                raise InfluxQueryError("influx_target_not_allowed", status_code=400)
            async with InfluxQueryClient(request, pinned_address=pinned_address) as client:
                return await client.query()
    except TimeoutError as exc:
        raise InfluxQueryError("influx_timeout", status_code=504) from exc
