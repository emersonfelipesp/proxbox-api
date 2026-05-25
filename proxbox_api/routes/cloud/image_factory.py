"""Packer-backed image factory routes (stateless — no DB persistence)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import timezone
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.schemas.image_factory import PackerImageBuildRequest, PackerImageBuildResponse
from proxbox_api.services.image_factory.logs import scrub_payload, scrub_text
from proxbox_api.services.image_factory.models import (
    LiveImageBuildRun,
    drop_live_run,
    get_live_run,
    register_live_run,
    response_from_live,
    utcnow,
)
from proxbox_api.services.image_factory.renderer import render_packer_workdir
from proxbox_api.services.image_factory.runner import (
    CommandResult,
    PackerCommandError,
    PackerRunner,
)
from proxbox_api.services.image_factory.workdir import cleanup_workdir, create_workdir
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()


def packer_runner_factory(*, env: dict[str, str], secrets: tuple[str, ...]) -> PackerRunner:
    return PackerRunner(env=env, secrets=secrets)


async def _gated_endpoint(
    session: SessionDep,
    endpoint_id: int,
) -> ProxmoxEndpoint | JSONResponse:
    gated = await _gate(session, endpoint_id)
    if not isinstance(gated, JSONResponse):
        return gated
    content = json.loads(gated.body.decode() or "{}")
    if content.get("reason") == "endpoint_not_found":
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=content)
    return gated


def _proxmox_api_url(endpoint: ProxmoxEndpoint) -> str:
    host = endpoint.domain or endpoint.ip_address.split("/")[0]
    return f"https://{host}:{endpoint.port}/api2/json"


def _packer_env(endpoint: ProxmoxEndpoint) -> tuple[dict[str, str], tuple[str, ...]]:
    token = endpoint.get_decrypted_token_value()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Proxmox API token credentials are required for Packer image builds.",
        )
    username = endpoint.username
    if endpoint.token_name and "!" not in username:
        username = f"{username}!{endpoint.token_name}"
    url = _proxmox_api_url(endpoint)
    env = {
        "PROXMOX_URL": url,
        "PROXMOX_USERNAME": username,
        "PROXMOX_TOKEN": token,
    }
    return env, (url, username, token)


async def _ensure_template_vmid_exists(
    endpoint: ProxmoxEndpoint,
    request: PackerImageBuildRequest,
) -> None:
    proxmox = None
    try:
        proxmox = await _open_proxmox_session(endpoint)
        config = await _maybe_await(
            proxmox.session.nodes(request.target_node).qemu(request.template_vmid).config.get()
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"template_vmid {request.template_vmid} was not found on "
                f"node {request.target_node}."
            ),
        ) from exc
    finally:
        if proxmox is not None:
            await proxmox.aclose()
    if not isinstance(config, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"template_vmid {request.template_vmid} did not return a VM config.",
        )


def _sse_frame(name: str, data: dict[str, Any], secrets: tuple[str, ...] = ()) -> str:
    return f"event: {name}\ndata: {json.dumps(scrub_payload(data, secrets))}\n\n"


def _result_summary(result: CommandResult) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "stdout_lines": len(result.stdout),
        "stderr_lines": len(result.stderr),
    }


def _validate_builder_fields(request: PackerImageBuildRequest) -> None:
    """Raise HTTPException if builder-type-specific required fields are missing."""
    if request.builder_type == "proxmox-clone":
        if request.template_vmid is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="template_vmid is required for proxmox-clone builder.",
            )
    elif request.builder_type == "proxmox-iso":
        missing = [f for f in ("iso_file", "iso_checksum") if not getattr(request, f)]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"ISO builder requires: {', '.join(missing)}.",
            )


async def _prepare_build(
    request: PackerImageBuildRequest,
    session: SessionDep,
    *,
    register_live: bool,
) -> tuple[str, LiveImageBuildRun, PackerImageBuildResponse]:
    _validate_builder_fields(request)
    endpoint_or_error = await _gated_endpoint(session, request.endpoint_id)
    if isinstance(endpoint_or_error, JSONResponse):
        raise _JsonResponseException(endpoint_or_error)
    endpoint = endpoint_or_error
    if request.builder_type == "proxmox-clone":
        await _ensure_template_vmid_exists(endpoint, request)
    env, secrets = _packer_env(endpoint)
    build_id = str(uuid4())
    workdir = create_workdir(build_id)
    try:
        rendered = render_packer_workdir(request=request, workdir=workdir)
        runner = packer_runner_factory(env=env, secrets=secrets)
        live = LiveImageBuildRun(
            build_id=build_id,
            request=request,
            rendered=rendered,
            runner=runner,
        )
        if register_live:
            await register_live_run(live)
        return build_id, live, response_from_live(live)
    except Exception:
        cleanup_workdir(workdir)
        raise


class _JsonResponseException(Exception):
    def __init__(self, response: JSONResponse) -> None:
        self.response = response


async def _run_validation(
    *,
    session: SessionDep,
    live: LiveImageBuildRun,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    started_at = utcnow()
    init_result = await live.runner.init(live.rendered.workdir)
    validate_result = await live.runner.validate(live.rendered.workdir, live.rendered.var_file)
    completed_at = utcnow()
    valid = init_result.exit_code == 0 and validate_result.exit_code == 0
    status_value = "completed" if valid else "failed"
    error = None
    if not valid:
        output = (
            init_result.stderr
            + validate_result.stderr
            + init_result.stdout
            + validate_result.stdout
        )
        error = scrub_text("\n".join(output) or "packer validate failed", secrets)
    cleanup_workdir(live.rendered.workdir, keep_workdir=live.keep_workdir)
    return {
        "build_id": live.build_id,
        "valid": valid,
        "status": status_value,
        "error": error,
        "init": _result_summary(init_result),
        "validate": _result_summary(validate_result),
        "response": response_from_live(
            live,
            status=status_value,
            started_at=started_at,
            completed_at=completed_at,
        ).model_dump(mode="json"),
    }


def _extract_host(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or url


@router.get("/proxmox-endpoint/by-url")
async def get_endpoint_by_url(
    url: str = Query(..., description="Proxmox endpoint URL to look up"),
    session: SessionDep = ...,
) -> dict[str, Any]:
    """Resolve a Proxmox endpoint URL to its proxbox-api endpoint ID."""
    from sqlmodel import select as sql_select

    host = _extract_host(url)
    result = await _maybe_await(
        session.exec(
            sql_select(ProxmoxEndpoint).where(
                (ProxmoxEndpoint.domain == host) | (ProxmoxEndpoint.ip_address == host)
            )
        )
    )
    endpoint = result.first()
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No ProxmoxEndpoint matching URL '{url}'.",
        )
    return {"id": endpoint.id, "name": endpoint.name or "", "url": _proxmox_api_url(endpoint)}


@router.post(
    "/image-factory/builds",
    response_model=PackerImageBuildResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_image_factory_build(
    request: PackerImageBuildRequest,
    session: SessionDep,
) -> PackerImageBuildResponse | JSONResponse:
    try:
        build_id, live, response = await _prepare_build(request, session, register_live=True)
    except _JsonResponseException as exc:
        return exc.response
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if request.dry_run:
        secrets = live.runner.secrets
        result = await _run_validation(session=session, live=live, secrets=secrets)
        await drop_live_run(build_id)
        return response_from_live(live, status=result.get("status", "completed"))

    return response


@router.get(
    "/image-factory/builds/{build_id}",
    response_model=PackerImageBuildResponse,
)
async def get_image_factory_build(
    build_id: str,
    session: SessionDep,
) -> PackerImageBuildResponse | JSONResponse:
    live = await get_live_run(build_id)
    if live is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"No active image factory build with id={build_id}."},
        )
    endpoint_or_error = await _gated_endpoint(session, live.request.endpoint_id)
    if isinstance(endpoint_or_error, JSONResponse):
        return endpoint_or_error
    return response_from_live(live, status="running")


async def _build_stream_generator(
    *,
    build_id: str,
    session: SessionDep,
    live: LiveImageBuildRun,
    secrets: tuple[str, ...],
    keepalive_interval: float = 15.0,
) -> AsyncGenerator[str, None]:
    last_keepalive = asyncio.get_event_loop().time()

    def maybe_keepalive() -> str | None:
        nonlocal last_keepalive
        now = asyncio.get_event_loop().time()
        if now - last_keepalive >= keepalive_interval:
            last_keepalive = now
            return ": keepalive\n\n"
        return None

    try:
        started_at = utcnow()
        yield _sse_frame(
            "build_started",
            {
                "build_id": build_id,
                "endpoint_id": live.request.endpoint_id,
                "target_node": live.request.target_node,
                "output_vmid": live.request.output_vmid,
                "output_name": live.request.output_name,
                "started_at": started_at.astimezone(timezone.utc).isoformat(),
            },
            secrets,
        )

        init_result = await live.runner.init(live.rendered.workdir)
        yield _sse_frame(
            "packer_init", {"build_id": build_id, **_result_summary(init_result)}, secrets
        )
        if init_result.exit_code != 0:
            await _finish_failed(live, init_result.stderr or init_result.stdout, secrets)
            yield _sse_frame(
                "build_failed",
                {"build_id": build_id, "error": "packer init failed"},
                secrets,
            )
            yield _sse_frame("complete", {"build_id": build_id, "status": "failed"}, secrets)
            return

        validate_result = await live.runner.validate(live.rendered.workdir, live.rendered.var_file)
        yield _sse_frame(
            "packer_validate",
            {"build_id": build_id, **_result_summary(validate_result)},
            secrets,
        )
        if validate_result.exit_code != 0:
            await _finish_failed(
                live,
                validate_result.stderr or validate_result.stdout,
                secrets,
            )
            yield _sse_frame(
                "build_failed",
                {"build_id": build_id, "error": "packer validate failed"},
                secrets,
            )
            yield _sse_frame("complete", {"build_id": build_id, "status": "failed"}, secrets)
            return

        artifact_seen = False
        async for event in live.runner.build(live.rendered.workdir, live.rendered.var_file):
            artifact_seen = artifact_seen or event.name == "packer_artifact"
            yield _sse_frame(event.name, {"build_id": build_id, **event.data}, secrets)
            keepalive = maybe_keepalive()
            if keepalive is not None:
                yield keepalive

        if live.cancel_requested.is_set():
            await _finish_cancelled(live)
            yield _sse_frame("complete", {"build_id": build_id, "status": "cancelled"}, secrets)
            return

        artifact_data = {
            "build_id": build_id,
            "template_name": live.request.output_name,
            "vmid": live.request.output_vmid,
        }
        if not artifact_seen:
            yield _sse_frame("packer_artifact", artifact_data, secrets)
        completed_at = utcnow()
        cleanup_workdir(live.rendered.workdir, keep_workdir=live.keep_workdir)
        await drop_live_run(build_id)
        yield _sse_frame(
            "build_completed",
            response_from_live(
                live,
                status="completed",
                started_at=started_at,
                completed_at=completed_at,
            ).model_dump(mode="json"),
            secrets,
        )
        yield _sse_frame("complete", {"build_id": build_id, "status": "completed"}, secrets)
    except asyncio.CancelledError:
        return
    except PackerCommandError as exc:
        if live.cancel_requested.is_set():
            await _finish_cancelled(live)
            yield _sse_frame("complete", {"build_id": build_id, "status": "cancelled"}, secrets)
            return
        await _finish_failed(live, exc.output or [str(exc)], secrets, exit_code=exc.exit_code)
        yield _sse_frame(
            "build_failed",
            {"build_id": build_id, "error": scrub_text(str(exc), secrets)},
            secrets,
        )
        yield _sse_frame("complete", {"build_id": build_id, "status": "failed"}, secrets)
    except Exception as exc:  # noqa: BLE001
        await _finish_failed(live, [str(exc)], secrets)
        yield _sse_frame(
            "build_failed",
            {"build_id": build_id, "error": scrub_text(str(exc), secrets)},
            secrets,
        )
        yield _sse_frame("complete", {"build_id": build_id, "status": "failed"}, secrets)


async def _finish_failed(
    live: LiveImageBuildRun,
    output: list[str],
    secrets: tuple[str, ...],
    *,
    exit_code: int | None = None,
) -> None:
    cleanup_workdir(live.rendered.workdir, keep_workdir=live.keep_workdir)
    await drop_live_run(live.build_id)


async def _finish_cancelled(live: LiveImageBuildRun) -> None:
    cleanup_workdir(live.rendered.workdir, keep_workdir=live.keep_workdir)
    await drop_live_run(live.build_id)


@router.get("/image-factory/builds/{build_id}/stream", response_model=None)
async def stream_image_factory_build(
    build_id: str,
    session: SessionDep,
) -> StreamingResponse | JSONResponse:
    live = await get_live_run(build_id)
    if live is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"No active image factory build with id={build_id}."},
        )
    endpoint_or_error = await _gated_endpoint(session, live.request.endpoint_id)
    if isinstance(endpoint_or_error, JSONResponse):
        return endpoint_or_error
    _, secrets = _packer_env(endpoint_or_error)
    return StreamingResponse(
        _build_stream_generator(
            build_id=build_id,
            session=session,
            live=live,
            secrets=secrets,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post(
    "/image-factory/builds/{build_id}/cancel",
    response_model=PackerImageBuildResponse,
)
async def cancel_image_factory_build(
    build_id: str,
    session: SessionDep,
) -> PackerImageBuildResponse | JSONResponse:
    live = await get_live_run(build_id)
    if live is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": f"No active image factory build with id={build_id}."},
        )
    endpoint_or_error = await _gated_endpoint(session, live.request.endpoint_id)
    if isinstance(endpoint_or_error, JSONResponse):
        return endpoint_or_error
    live.cancel_requested.set()
    await live.runner.cancel(build_id)
    cleanup_workdir(live.rendered.workdir, keep_workdir=live.keep_workdir)
    await drop_live_run(build_id)
    return response_from_live(live, status="cancelled", completed_at=utcnow())


@router.post("/image-factory/validate", response_model=None)
async def validate_image_factory_build(
    request: PackerImageBuildRequest,
    session: SessionDep,
) -> dict[str, Any] | JSONResponse:
    try:
        build_id, live, _response = await _prepare_build(request, session, register_live=False)
    except _JsonResponseException as exc:
        return exc.response
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    try:
        secrets = live.runner.secrets
        return await _run_validation(session=session, live=live, secrets=secrets)
    finally:
        await drop_live_run(build_id)
