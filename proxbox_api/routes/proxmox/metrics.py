"""Authenticated, provider-neutral Proxmox metrics query routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.schemas.influx import InfluxQueryRequest, InfluxQueryResponse
from proxbox_api.schemas.proxmox_metric_servers import (
    CONFIG_ID_PATTERN,
    InfluxMetricServerUpdate,
    InfluxMetricServerWriteResponse,
)
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


def _writes_error(status_code: int, reason: str, **extra: object) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"reason": reason, **extra})


async def _close_session(proxmox: object) -> None:
    close_method = getattr(proxmox, "aclose", None)
    if callable(close_method):
        try:
            await close_method()
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.debug("Failed to close metric-server write Proxmox session")


@router.put("/influx/servers/{config_id}", response_model=InfluxMetricServerWriteResponse)
async def update_influx_metric_server(
    config_id: Annotated[str, Path(pattern=CONFIG_ID_PATTERN)],
    payload: InfluxMetricServerUpdate,
    database_session: AsyncDatabaseSessionDep,
    endpoint_id: Annotated[int | None, Query()] = None,
    actor: Annotated[str | None, Header(alias="X-Proxbox-Actor")] = None,
) -> InfluxMetricServerWriteResponse | JSONResponse:
    """Update one Proxmox InfluxDB metric server on a write-enabled endpoint.

    Requires an explicit ``endpoint_id`` with ``allow_writes`` enabled and the
    ``X-Proxbox-Actor`` header. The body is allow-listed; the token and the
    submitted values are never logged or returned.
    """
    actor_value = (actor or "").strip()
    if not actor_value:
        raise _writes_error(
            422,
            "actor_required",
            detail="Metric-server writes require X-Proxbox-Actor.",
        )
    body = payload.pve_payload()
    endpoint = await _gate(database_session, endpoint_id)
    if isinstance(endpoint, JSONResponse):
        return endpoint

    path = f"cluster/metrics/server/{config_id}"
    fields = ",".join(payload.field_names())
    # Audit the attempt before dispatch: Proxmox can apply part of an update
    # (for example a new token) and then fail its connectivity check.
    logger.info(
        "Proxmox metric-server write attempt: actor=%s endpoint=%s config=%s fields=%s",
        actor_value,
        endpoint.id,
        config_id,
        fields,
    )
    try:
        proxmox = await _open_proxmox_session(endpoint)
    except Exception:
        logger.warning(
            "Proxmox metric-server write not applied (no session): actor=%s endpoint=%s config=%s",
            actor_value,
            endpoint.id,
            config_id,
        )
        raise _writes_error(
            status.HTTP_502_BAD_GATEWAY,
            "proxmox_session_unreachable",
            endpoint_id=endpoint.id,
        ) from None
    try:
        await resolve_async(proxmox.session(path).put(**body))
    except BaseException as error:
        logger.warning(
            "Proxmox metric-server write failed, possibly partially applied: "
            "actor=%s endpoint=%s config=%s fields=%s",
            actor_value,
            endpoint.id,
            config_id,
            fields,
        )
        if isinstance(error, Exception):
            raise _writes_error(
                status.HTTP_502_BAD_GATEWAY,
                "proxmox_metric_server_write_failed",
                endpoint_id=endpoint.id,
                detail="The update may have been partially applied; verify the metric server.",
            ) from None
        raise
    finally:
        await _close_session(proxmox)

    logger.info(
        "Proxmox metric-server write applied: actor=%s endpoint=%s config=%s fields=%s",
        actor_value,
        endpoint.id,
        config_id,
        fields,
    )
    return InfluxMetricServerWriteResponse(
        status="pushed",
        endpoint_id=endpoint.id,
        cluster_name=endpoint.name,
        actor=actor_value,
        path=path,
        fields=payload.field_names(),
    )
