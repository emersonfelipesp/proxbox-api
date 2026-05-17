"""Image factory live-state and persistence helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from proxbox_api.database import ImageBuildRun
from proxbox_api.schemas.image_factory import PackerImageBuildRequest, PackerImageBuildResponse
from proxbox_api.services.image_factory.renderer import RenderedPackerWorkdir
from proxbox_api.services.image_factory.runner import PackerRunner
from proxbox_api.utils.async_compat import maybe_await as _maybe_await


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


async def create_image_build_run(
    session: object,
    *,
    build_id: str,
    request: PackerImageBuildRequest,
    workdir: Path,
    template_path: Path,
    status: str = "queued",
) -> ImageBuildRun:
    run = ImageBuildRun(
        id=build_id,
        uuid=build_id,
        status=status,
        endpoint_id=request.endpoint_id,
        target_node=request.target_node,
        builder_type=request.builder_type,
        source_template_vmid=request.template_vmid,
        output_vmid=request.output_vmid,
        output_name=request.output_name,
        os_family=request.os_family,
        os_release=request.os_release,
        image_version=request.image_version,
        workdir=str(workdir),
        artifact_metadata={
            "template_name": request.output_name,
            "packer_template_path": str(template_path),
            "provisioner_recipe": request.provisioner_recipe,
        },
    )
    session.add(run)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(run))
    return run


async def get_image_build_run(session: object, build_id: str) -> ImageBuildRun | None:
    return await _maybe_await(session.get(ImageBuildRun, build_id))  # type: ignore[attr-defined]


async def update_image_build_run(
    session: object,
    build_id: str,
    **fields: Any,
) -> ImageBuildRun | None:
    run = await get_image_build_run(session, build_id)
    if run is None:
        return None
    for key, value in fields.items():
        setattr(run, key, value)
    session.add(run)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(run))
    return run


def response_from_run(run: ImageBuildRun) -> PackerImageBuildResponse:
    metadata = run.artifact_metadata or {}
    return PackerImageBuildResponse(
        build_id=run.id,
        status=run.status,  # type: ignore[arg-type]
        endpoint_id=run.endpoint_id,
        target_node=run.target_node,
        output_vmid=run.output_vmid,
        output_name=run.output_name,
        artifact_template_name=metadata.get("template_name"),
        packer_template_path=metadata.get("packer_template_path"),
        started_at=run.started_at,
        completed_at=run.completed_at,
        log_url=f"/cloud/image-factory/builds/{run.id}/stream",
    )
