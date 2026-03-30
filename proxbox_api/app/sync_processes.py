"""NetBox plugin sync-process REST helpers mounted on the root app."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from proxbox_api.app.netbox_session import get_raw_netbox_session
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_create, rest_list

sync_process_router = APIRouter()


class SyncProcessIn(BaseModel):
    name: str
    sync_type: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None


class SyncProcess(SyncProcessIn):
    id: int
    url: str
    display: str


example_sync_process = SyncProcess(
    id=1,
    url="https://10.0.30.200/api/plugins/proxbox/sync-processes/1/",
    display="teste (all)",
    name="teste",
    sync_type="all",
    status="not-started",
    started_at="2025-03-13T15:08:09.051478Z",
    completed_at="2025-03-13T15:08:09.051478Z",
)


@sync_process_router.get("/sync-processes", response_model=list[SyncProcess])
async def get_sync_processes() -> list[dict]:
    """Return all sync processes from the NetBox Proxbox plugin."""
    nb = get_raw_netbox_session()
    if nb is None:
        raise ProxboxException(message="Failed to establish NetBox session")

    try:
        return [
            process.serialize() for process in rest_list(nb, "/api/plugins/proxbox/sync-processes/")
        ]
    except Exception as error:  # noqa: BLE001
        raise ProxboxException(message="Error fetching sync processes", python_exception=str(error)) from error


@sync_process_router.post("/sync-processes", response_model=SyncProcess)
async def create_sync_process() -> dict:
    """Create a new sync process record in NetBox."""
    logger.debug("create_sync_process at %s", datetime.now())

    nb = get_raw_netbox_session()
    if nb is None:
        raise ProxboxException(message="Failed to establish NetBox session")

    try:
        return rest_create(
            nb,
            "/api/plugins/proxbox/sync-processes/",
            {
                "name": f"sync-process-{datetime.now()}",
                "sync_type": "all",
                "status": "not-started",
                "started_at": str(datetime.now()),
                "completed_at": str(datetime.now()),
            },
        )
    except Exception as error:  # noqa: BLE001
        raise ProxboxException(message="Error creating sync process", python_exception=str(error)) from error


def register_sync_process_routes(app) -> None:
    """Mount sync-process routes on the root application."""
    app.include_router(sync_process_router)
