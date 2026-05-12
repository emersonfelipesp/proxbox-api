"""``GET /sync/active`` — soft probe for in-flight full-update runs."""

from __future__ import annotations

from fastapi import APIRouter

from proxbox_api.app.sync_state import get_active_sync
from proxbox_api.schemas.sync import SyncActiveResponse

router = APIRouter()


@router.get("/sync/active", response_model=SyncActiveResponse, tags=["sync"])
async def sync_active() -> SyncActiveResponse:
    """Report whether this API replica is currently running a sync.

    The registry is in-memory and process-local, so this endpoint is a soft
    probe — useful for single-replica deployments and for cron/single-exec
    callers that want to fast-fail when a sync is already in flight. Operators
    running multiple uvicorn workers should treat conflicting answers across
    workers as expected.
    """
    payload = await get_active_sync()
    return SyncActiveResponse.model_validate(payload)
