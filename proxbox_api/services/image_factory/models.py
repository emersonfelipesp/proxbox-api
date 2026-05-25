"""Image factory live-state helpers (stateless — no DB persistence)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from proxbox_api.schemas.image_factory import PackerImageBuildRequest, PackerImageBuildResponse
from proxbox_api.services.image_factory.renderer import RenderedPackerWorkdir
from proxbox_api.services.image_factory.runner import PackerRunner


@dataclass
class LiveImageBuildRun:
    build_id: str
    request: PackerImageBuildRequest
    rendered: RenderedPackerWorkdir
    runner: PackerRunner
    keep_workdir: bool = False
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)


_LIVE_RUNS: dict[str, LiveImageBuildRun] = {}
_LIVE_RUNS_LOCK = asyncio.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def register_live_run(run: LiveImageBuildRun) -> None:
    async with _LIVE_RUNS_LOCK:
        _LIVE_RUNS[run.build_id] = run


async def get_live_run(build_id: str) -> LiveImageBuildRun | None:
    async with _LIVE_RUNS_LOCK:
        return _LIVE_RUNS.get(build_id)


async def drop_live_run(build_id: str) -> None:
    async with _LIVE_RUNS_LOCK:
        _LIVE_RUNS.pop(build_id, None)


def response_from_live(
    live: LiveImageBuildRun,
    *,
    status: str = "queued",
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> PackerImageBuildResponse:
    return PackerImageBuildResponse(
        build_id=live.build_id,
        status=status,  # type: ignore[arg-type]
        endpoint_id=live.request.endpoint_id,
        target_node=live.request.target_node,
        output_vmid=live.request.output_vmid,
        output_name=live.request.output_name,
        artifact_template_name=live.request.output_name,
        packer_template_path=str(live.rendered.template_path),
        started_at=started_at,
        completed_at=completed_at,
        log_url=f"/cloud/image-factory/builds/{live.build_id}/stream",
    )
