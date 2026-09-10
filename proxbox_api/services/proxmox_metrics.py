"""Bounded normalization for Proxmox cluster metric pulls."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from typing import Any, cast

from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.proxmox_async import resolve_async
from proxbox_api.schemas.proxmox_metrics import (
    MAX_PULL_TIMESTAMP,
    ProxmoxMetricSample,
    ProxmoxMetricsPullRequest,
    ProxmoxMetricsPullResponse,
    ProxmoxMetricsPullWindow,
)
from proxbox_api.services.proxmox_bounded import (
    ProxmoxResponseTooLargeError,
    bounded_proxmox_get,
)
from proxbox_api.session.proxmox import ProxmoxSession, resolve_proxmox_target_session

MAX_PROVIDER_ITEMS = 50_000
logger = logging.getLogger(__name__)
PULL_COLUMNS = [
    "object_id",
    "metric",
    "timestamp",
    "value",
    "metric_type",
    "source",
]


class ProxmoxMetricsPullError(Exception):
    """Secret-safe failure raised by the Proxmox pull boundary."""

    def __init__(self, reason: str, *, status_code: int = 502) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def _provider_rows(payload: object) -> list[object]:
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        payload = model_dump()
    if isinstance(payload, dict):
        payload = cast(dict[str, object], payload).get("data")
    if not isinstance(payload, list) or len(payload) > MAX_PROVIDER_ITEMS:
        raise ProxmoxMetricsPullError("pull_invalid_response")
    return cast(list[object], payload)


def _bounded_payload_size(payload: object, maximum: int) -> None:
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProxmoxMetricsPullError("pull_invalid_response") from exc
    if len(encoded) > maximum:
        raise ProxmoxMetricsPullError("pull_response_too_large")


def _valid_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= maximum


def _valid_timestamp(value: object) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_PULL_TIMESTAMP
    )


def _valid_metric_value(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _sample_from_row(row: object) -> ProxmoxMetricSample:
    if not isinstance(row, dict):
        raise ProxmoxMetricsPullError("pull_invalid_response")
    row_data = cast(dict[str, object], row)
    object_id = row_data.get("id")
    metric = row_data.get("metric")
    timestamp = row_data.get("timestamp")
    value = row_data.get("value")
    metric_type = row_data.get("type")
    valid = all(
        (
            _valid_text(object_id, 256),
            _valid_text(metric, 128),
            _valid_timestamp(timestamp),
            _valid_metric_value(value),
            metric_type in {None, "gauge", "counter", "derive"},
        )
    )
    if not valid:
        raise ProxmoxMetricsPullError("pull_invalid_response")
    return ProxmoxMetricSample(
        object_id=cast(str, object_id),
        metric=cast(str, metric),
        timestamp=cast(int, timestamp),
        value=float(cast(int | float, value)),
        metric_type=cast(Any, metric_type),
    )


def _matches_request(sample: ProxmoxMetricSample, request: ProxmoxMetricsPullRequest) -> bool:
    if request.start_time is not None and sample.timestamp <= request.start_time:
        return False
    if request.object_ids and sample.object_id not in request.object_ids:
        return False
    if request.object_prefixes and not any(
        sample.object_id == prefix or sample.object_id.startswith(f"{prefix}/")
        for prefix in request.object_prefixes
    ):
        return False
    return not request.metric_names or sample.metric in request.metric_names


def _filtered_samples(
    provider_rows: list[object], request: ProxmoxMetricsPullRequest
) -> list[ProxmoxMetricSample]:
    samples = [_sample_from_row(row) for row in provider_rows]
    return [sample for sample in samples if _matches_request(sample, request)]


def _deduplicate_samples(
    samples: list[ProxmoxMetricSample],
) -> tuple[list[ProxmoxMetricSample], int]:
    unique: dict[tuple[str, str, int], ProxmoxMetricSample] = {}
    duplicate_count = 0
    for sample in samples:
        identity = (sample.object_id, sample.metric, sample.timestamp)
        previous = unique.get(identity)
        if previous is None:
            unique[identity] = sample
            continue
        if previous.value != sample.value or previous.metric_type != sample.metric_type:
            raise ProxmoxMetricsPullError("pull_invalid_response")
        duplicate_count += 1
    return list(unique.values()), duplicate_count


def _window_start(
    request: ProxmoxMetricsPullRequest,
    samples: list[ProxmoxMetricSample],
    captured: datetime,
) -> int:
    if request.start_time is not None:
        return request.start_time
    if samples:
        return samples[0].timestamp
    return int(captured.timestamp())


def normalize_proxmox_metrics(
    payload: object,
    request: ProxmoxMetricsPullRequest,
    *,
    captured_at: datetime | None = None,
) -> ProxmoxMetricsPullResponse:
    """Validate and normalize one Proxmox metrics-export response."""
    provider_rows = _provider_rows(payload)
    _bounded_payload_size(provider_rows, request.max_response_bytes)
    samples = _filtered_samples(provider_rows, request)
    samples.sort(key=lambda item: (item.timestamp, item.object_id, item.metric))
    ordered, duplicate_count = _deduplicate_samples(samples)
    truncated = len(ordered) > request.max_rows
    ordered = ordered[: request.max_rows]
    captured = captured_at or datetime.now(UTC)
    start_timestamp = _window_start(request, ordered, captured)
    window = ProxmoxMetricsPullWindow(
        start=datetime.fromtimestamp(start_timestamp, UTC).isoformat(),
        stop=captured.isoformat(),
    )
    return ProxmoxMetricsPullResponse(
        columns=PULL_COLUMNS,
        rows=[sample.model_dump(mode="json") for sample in ordered],
        row_count=len(ordered),
        truncated=truncated,
        query_window=window,
        captured_at=captured,
        deduplicated_count=duplicate_count,
    )


async def execute_proxmox_metrics_pull(
    request: ProxmoxMetricsPullRequest,
    database_session: AsyncSession,
) -> ProxmoxMetricsPullResponse:
    """Call only Proxmox cluster metrics export and normalize its response."""
    target: ProxmoxSession | None = None
    try:
        target = await resolve_proxmox_target_session(
            database_session=database_session,
            source=request.source,
            endpoint_id=request.endpoint_id,
            name=request.target_name,
            domain=request.target_domain,
            ip_address=request.target_ip_address,
        )
        parameters: dict[str, object] = {
            "history": request.history,
            "local-only": request.local_only,
        }
        if request.start_time is not None:
            parameters["start-time"] = request.start_time
        sdk_session = target.session
        if sdk_session is None:
            raise ProxmoxMetricsPullError("pull_unavailable", status_code=503)
        resource: Any = sdk_session("cluster/metrics/export")
        payload = await resolve_async(
            bounded_proxmox_get(resource, request.max_response_bytes, **parameters)
        )
        return normalize_proxmox_metrics(payload, request)
    except ProxmoxResponseTooLargeError as exc:
        raise ProxmoxMetricsPullError("pull_response_too_large") from exc
    except ProxmoxMetricsPullError:
        raise
    except Exception as exc:
        raise ProxmoxMetricsPullError("pull_unavailable", status_code=503) from exc
    finally:
        if target is not None:
            try:
                await target.aclose()
            except Exception:
                logger.warning("Failed to close a Proxmox metrics pull session")
