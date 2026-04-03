"""Backend logs API endpoint for retrieving in-memory log buffer."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Query

from proxbox_api.log_buffer import LogLevel, get_logs

router = APIRouter()


@router.get("/logs")
async def get_backend_logs(
    level: Annotated[
        str | None,
        Query(
            title="Minimum Log Level",
            description="Filter logs by minimum level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            title="Limit",
            description="Maximum number of log entries to return",
            ge=1,
            le=5000,
        ),
    ] = 200,
    offset: Annotated[
        int,
        Query(
            title="Offset",
            description="Number of entries to skip for pagination",
            ge=0,
        ),
    ] = 0,
    since: Annotated[
        datetime | None,
        Query(
            title="Since",
            description="Only return logs after this ISO timestamp",
        ),
    ] = None,
    operation_id: Annotated[
        str | None,
        Query(
            title="Operation ID",
            description="Filter logs by specific operation ID (from sync jobs)",
        ),
    ] = None,
) -> dict:
    """Retrieve logs from the backend log buffer.

    This endpoint returns log entries stored in memory by the LogBufferHandler.
    Logs are stored in a circular buffer (max 10,000 entries) and are lost on restart.

    Filtering:
    - By level: Returns logs at or above the specified level
    - By since: Returns only logs after the specified timestamp
    - By operation_id: Returns only logs for a specific sync operation

    Query parameters can be combined. If filtering is active, the response
    will include an `active_filters` object indicating what filters are applied.

    Returns:
        Dictionary containing:
        - logs: List of log entry dictionaries
        - total: Total number of logs matching filters
        - has_more: Whether more logs exist beyond the current page
        - active_filters: Object showing which filters are currently applied
    """
    if level:
        try:
            LogLevel.from_string(level)
        except ValueError:
            level = None

    if since and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    result = get_logs(
        level=level,
        limit=limit,
        offset=offset,
        since=since,
        operation_id=operation_id,
    )

    return result
