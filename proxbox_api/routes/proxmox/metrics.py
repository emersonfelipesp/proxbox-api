"""Authenticated, provider-neutral Proxmox metrics query routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from proxbox_api.database import AsyncDatabaseSessionDep
from proxbox_api.schemas.influx import InfluxQueryRequest, InfluxQueryResponse
from proxbox_api.schemas.proxmox_metrics import (
    ProxmoxMetricsPullRequest,
    ProxmoxMetricsPullResponse,
)
from proxbox_api.services.influx import InfluxQueryError, execute_influx_query
from proxbox_api.services.proxmox_metrics import (
    ProxmoxMetricsPullError,
    execute_proxmox_metrics_pull,
)

router = APIRouter()

_PUBLIC_ERROR_MESSAGES = {
    "influx_connection_error": "InfluxDB could not be reached.",
    "influx_empty_response": "InfluxDB returned an empty response.",
    "influx_invalid_response": "InfluxDB returned an unsupported response.",
    "influx_response_too_large": "InfluxDB response exceeded the configured bound.",
    "influx_timeout": "InfluxDB query timed out.",
    "influx_tls_error": "InfluxDB TLS negotiation failed.",
    "influx_upstream_error": "InfluxDB rejected the query.",
    "influx_target_not_allowed": "The configured InfluxDB target is not allowed.",
    "pull_invalid_response": "Proxmox returned an unsupported metrics response.",
    "pull_response_too_large": "Proxmox metrics exceeded the configured bound.",
    "pull_unavailable": "Proxmox metrics could not be retrieved.",
}


@router.post("/influx/query", response_model=InfluxQueryResponse)
async def query_influx_metrics(payload: InfluxQueryRequest) -> InfluxQueryResponse:
    """Run one bounded Flux query against the caller-selected InfluxDB v2 host."""
    try:
        return await execute_influx_query(payload)
    except InfluxQueryError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "reason": exc.reason,
                "message": _PUBLIC_ERROR_MESSAGES[exc.reason],
            },
        ) from None


@router.post("/pull/query", response_model=ProxmoxMetricsPullResponse)
async def query_proxmox_metrics(
    payload: ProxmoxMetricsPullRequest,
    database_session: AsyncDatabaseSessionDep,
) -> ProxmoxMetricsPullResponse:
    """Pull one bounded metric set from Proxmox cluster metrics export."""
    try:
        return await execute_proxmox_metrics_pull(payload, database_session)
    except ProxmoxMetricsPullError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "reason": exc.reason,
                "message": _PUBLIC_ERROR_MESSAGES[exc.reason],
            },
        ) from None
