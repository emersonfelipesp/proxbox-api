"""Cloud Firecracker provisioning routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from proxbox_api.firecracker_agent.client import (
    FirecrackerHostAgentClient,
    FirecrackerHostAgentError,
)
from proxbox_api.logger import logger
from proxbox_api.schemas.firecracker import (
    FirecrackerAssetPrepareRequest,
    FirecrackerMicroVMAction,
    FirecrackerMicroVMCreateRequest,
    FirecrackerProvisionRequest,
    FirecrackerProvisionResponse,
)
from proxbox_api.utils.log_scrubbing import scrub_cloud_init
from proxbox_api.utils.streaming import sse_event

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


async def _run_firecracker_provision(
    req: FirecrackerProvisionRequest,
    *,
    actor: str | None = None,
    emit: Callable[[str, dict], Awaitable[None]] | None = None,
) -> FirecrackerProvisionResponse:
    async def send(event: str, payload: dict) -> None:
        if emit is not None:
            await emit(event, payload)

    client = FirecrackerHostAgentClient(
        req.host_agent_base_url,
        token=req.host_agent_token,
    )
    metadata = dict(req.metadata)
    metadata.update(
        {
            "actor": actor or "proxbox-api",
            "tenant_id": req.tenant_id,
            "netbox_microvm_id": req.netbox_microvm_id,
            "instance_ref": req.instance_ref,
        }
    )

    await send(
        "provision_step",
        {"step": "host_agent_health", "label": "Check Firecracker host", "status": "started"},
    )
    health = await client.health()
    if not health.ok:
        raise FirecrackerHostAgentError(f"host-agent is not healthy: {health.status}")
    await send(
        "provision_step",
        {"step": "host_agent_health", "label": "Check Firecracker host", "status": "done"},
    )

    await send(
        "provision_step",
        {"step": "capabilities", "label": "Read host capacity", "status": "started"},
    )
    capabilities = await client.capabilities()
    if req.network.mode.value == "nat" and not capabilities.supports_nat:
        raise FirecrackerHostAgentError("host-agent does not support NAT networking")
    if req.network.mode.value == "bridge" and not capabilities.supports_bridge:
        raise FirecrackerHostAgentError("host-agent does not support bridged networking")
    if capabilities.available_vcpus and req.vcpus > capabilities.available_vcpus:
        raise FirecrackerHostAgentError("host-agent does not have enough available vCPUs")
    if capabilities.available_memory_mib and req.memory_mib > capabilities.available_memory_mib:
        raise FirecrackerHostAgentError("host-agent does not have enough available memory")
    await send(
        "provision_step",
        {"step": "capabilities", "label": "Read host capacity", "status": "done"},
    )

    await send(
        "provision_step",
        {"step": "prepare_assets", "label": "Prepare kernel and rootfs", "status": "started"},
    )
    assets = await client.prepare_assets(FirecrackerAssetPrepareRequest(image=req.image))
    await send(
        "terminal_line",
        {
            "line": (
                "Prepared Firecracker assets "
                f"kernel={assets.kernel_image_path} rootfs={assets.rootfs_image_path}"
            )
        },
    )
    await send(
        "provision_step",
        {"step": "prepare_assets", "label": "Prepare kernel and rootfs", "status": "done"},
    )

    await send(
        "provision_step",
        {"step": "create_microvm", "label": "Create micro-VM", "status": "started"},
    )
    state = await client.create_microvm(
        FirecrackerMicroVMCreateRequest(
            microvm_id=req.microvm_id,
            name=req.name,
            image=req.image,
            network=req.network,
            vcpus=req.vcpus,
            memory_mib=req.memory_mib,
            disk_mib=req.disk_mib,
            ssh_authorized_keys=req.ssh_authorized_keys,
            metadata=metadata,
        )
    )
    await send(
        "provision_step",
        {"step": "create_microvm", "label": "Create micro-VM", "status": "done"},
    )

    if req.start_after_provision:
        await send(
            "provision_step",
            {"step": "start_microvm", "label": "Start micro-VM", "status": "started"},
        )
        state = await client.action(req.microvm_id, FirecrackerMicroVMAction.start)
        await send(
            "provision_step",
            {"step": "start_microvm", "label": "Start micro-VM", "status": "done"},
        )
    else:
        await send(
            "provision_step",
            {"step": "start_microvm", "label": "Start micro-VM", "status": "skipped"},
        )

    return FirecrackerProvisionResponse(
        ok=True,
        microvm_id=req.microvm_id,
        instance_ref=req.instance_ref,
        host_id=req.host_id,
        host_pool_id=req.host_pool_id,
        image_id=req.image.image_id,
        status=state.status,
        guest_ip=state.guest_ip,
    )


async def _firecracker_provision_stream_generator(
    req: FirecrackerProvisionRequest,
    *,
    actor: str | None = None,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[str] = asyncio.Queue()

    async def emit(event: str, payload: dict) -> None:
        await queue.put(sse_event(event, payload))

    async def run() -> None:
        try:
            response = await _run_firecracker_provision(req, actor=actor, emit=emit)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            safe = scrub_cloud_init({"e": str(error)})["e"]
            logger.warning(
                "firecracker provision stream failed microvm=%s: %s", req.microvm_id, safe
            )
            await queue.put(sse_event("complete", {"ok": False, "error": safe}))
        else:
            await queue.put(sse_event("complete", response.model_dump(mode="json")))
        finally:
            await queue.put("")

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if item == "":
                break
            yield item
    except asyncio.CancelledError:
        task.cancel()
        yield sse_event("complete", {"ok": False, "error": "Stream cancelled."})
    finally:
        await asyncio.gather(task, return_exceptions=True)


@router.post("/firecracker/provision", response_model=FirecrackerProvisionResponse)
async def provision_firecracker_microvm(
    req: FirecrackerProvisionRequest,
    actor: Annotated[str | None, Header(alias="X-Proxbox-Actor")] = None,
) -> FirecrackerProvisionResponse:
    return await _run_firecracker_provision(req, actor=actor)


@router.post("/firecracker/provision/stream", response_model=None)
async def provision_firecracker_microvm_stream(
    req: FirecrackerProvisionRequest,
    actor: Annotated[str | None, Header(alias="X-Proxbox-Actor")] = None,
) -> StreamingResponse:
    return StreamingResponse(
        _firecracker_provision_stream_generator(req, actor=actor),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
