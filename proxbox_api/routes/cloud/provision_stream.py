"""Cloud Portal VM provisioning SSE stream route."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, StreamingResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.logger import logger
from proxbox_api.routes.cloud.provision import (
    _bind_allocated_ip_to_vm_best_effort,
    _clone_template_vm,
    _cloud_provision_gate,
    _configure_cloud_init_vm,
    _journal_provision_best_effort,
    _prepare_qemu_cloud_network_request,
    _release_cloud_network_lease,
    _require_resolved_cloud_network,
    _resize_vm_disk,
    _start_vm_after_provision,
)
from proxbox_api.routes.proxmox_actions import _open_proxmox_session
from proxbox_api.schemas.cloud_provision import (
    CloudVMProvisionRequest,
    CloudVMProvisionResponse,
)
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.utils.log_scrubbing import scrub_cloud_init
from proxbox_api.utils.streaming import sse_event

stream_router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

_STEPS = [
    ("clone_template", "Clone template VM"),
    ("configure_cloud_init", "Configure cloud-init"),
    ("resize_disk", "Resize VM disk"),
    ("start_vm", "Start virtual machine"),
]


@dataclass
class _StreamProvisionState:
    clone_upid: str | None = None
    config_upid: str | None = None
    resize_upid: str | None = None
    start_upid: str | None = None
    failed: bool = False


async def _clone_template_for_stream(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
) -> tuple[str | None, str | None]:
    try:
        return await _clone_template_vm(proxmox, req), None
    except Exception as error:  # noqa: BLE001
        return None, scrub_cloud_init({"e": str(error)})["e"]


async def _configure_cloud_init_for_stream(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
) -> tuple[str | None, str | None]:
    try:
        return await _configure_cloud_init_vm(proxmox, req), None
    except Exception as error:  # noqa: BLE001
        return None, scrub_cloud_init({"e": str(error)})["e"]


async def _resize_vm_disk_for_stream(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
) -> tuple[str | None, str | None]:
    try:
        return await _resize_vm_disk(proxmox, req), None
    except Exception as error:  # noqa: BLE001
        return None, scrub_cloud_init({"e": str(error)})["e"]


async def _start_vm_for_stream(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
) -> tuple[str | None, str | None]:
    try:
        return await _start_vm_after_provision(proxmox, req), None
    except Exception as error:  # noqa: BLE001
        return None, scrub_cloud_init({"e": str(error)})["e"]


async def _emit_clone_template_events(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
    state: _StreamProvisionState,
) -> AsyncIterator[str]:
    yield sse_event(
        "terminal_line",
        {
            "line": f"Cloning template VMID {req.template_vmid} → VMID {req.new_vmid} on {req.target_node}…"
        },
    )
    yield sse_event(
        "provision_step",
        {
            "step": "clone_template",
            "label": "Clone template VM",
            "status": "started",
        },
    )
    state.clone_upid, clone_error = await _clone_template_for_stream(proxmox, req)
    if clone_error is not None:
        state.failed = True
        yield sse_event(
            "provision_step",
            {
                "step": "clone_template",
                "label": "Clone template VM",
                "status": "error",
                "error": clone_error,
            },
        )
        yield sse_event("complete", {"ok": False, "error": clone_error, "step": "clone_template"})
        return

    yield sse_event("terminal_line", {"line": f"Clone task: {state.clone_upid or 'ok'}"})
    yield sse_event(
        "provision_step",
        {
            "step": "clone_template",
            "label": "Clone template VM",
            "status": "done",
            "upid": state.clone_upid,
        },
    )


async def _emit_configure_cloud_init_events(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
    state: _StreamProvisionState,
) -> AsyncIterator[str]:
    yield sse_event("terminal_line", {"line": "Applying cloud-init configuration…"})
    yield sse_event(
        "provision_step",
        {
            "step": "configure_cloud_init",
            "label": "Configure cloud-init",
            "status": "started",
        },
    )
    state.config_upid, config_error = await _configure_cloud_init_for_stream(proxmox, req)
    if config_error is not None:
        state.failed = True
        yield sse_event(
            "provision_step",
            {
                "step": "configure_cloud_init",
                "label": "Configure cloud-init",
                "status": "error",
                "error": config_error,
            },
        )
        yield sse_event(
            "complete", {"ok": False, "error": config_error, "step": "configure_cloud_init"}
        )
        return

    yield sse_event("terminal_line", {"line": f"Config task: {state.config_upid or 'ok'}"})
    yield sse_event(
        "provision_step",
        {
            "step": "configure_cloud_init",
            "label": "Configure cloud-init",
            "status": "done",
            "upid": state.config_upid,
        },
    )


async def _emit_resize_disk_events(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
    state: _StreamProvisionState,
) -> AsyncIterator[str]:
    if req.disk_gb is None:
        yield sse_event(
            "provision_step",
            {
                "step": "resize_disk",
                "label": "Resize VM disk",
                "status": "skipped",
            },
        )
        return

    yield sse_event(
        "terminal_line",
        {"line": f"Resizing scsi0 to {req.disk_gb}G…"},
    )
    yield sse_event(
        "provision_step",
        {
            "step": "resize_disk",
            "label": "Resize VM disk",
            "status": "started",
        },
    )
    state.resize_upid, resize_error = await _resize_vm_disk_for_stream(proxmox, req)
    if resize_error is not None:
        state.failed = True
        yield sse_event(
            "provision_step",
            {
                "step": "resize_disk",
                "label": "Resize VM disk",
                "status": "error",
                "error": resize_error,
            },
        )
        yield sse_event("complete", {"ok": False, "error": resize_error, "step": "resize_disk"})
        return

    yield sse_event("terminal_line", {"line": f"Resize task: {state.resize_upid or 'ok'}"})
    yield sse_event(
        "provision_step",
        {
            "step": "resize_disk",
            "label": "Resize VM disk",
            "status": "done",
            "upid": state.resize_upid,
        },
    )


async def _emit_start_vm_events(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
    state: _StreamProvisionState,
) -> AsyncIterator[str]:
    if not req.start_after_provision:
        yield sse_event(
            "provision_step",
            {
                "step": "start_vm",
                "label": "Start virtual machine",
                "status": "skipped",
            },
        )
        return

    yield sse_event("terminal_line", {"line": f"Starting VM {req.new_vmid}…"})
    yield sse_event(
        "provision_step",
        {
            "step": "start_vm",
            "label": "Start virtual machine",
            "status": "started",
        },
    )
    state.start_upid, start_error = await _start_vm_for_stream(proxmox, req)
    if start_error is not None:
        state.failed = True
        yield sse_event(
            "provision_step",
            {
                "step": "start_vm",
                "label": "Start virtual machine",
                "status": "error",
                "error": start_error,
            },
        )
        yield sse_event("complete", {"ok": False, "error": start_error, "step": "start_vm"})
        return

    yield sse_event("terminal_line", {"line": f"Start task: {state.start_upid or 'ok'}"})
    yield sse_event(
        "provision_step",
        {
            "step": "start_vm",
            "label": "Start virtual machine",
            "status": "done",
            "upid": state.start_upid,
        },
    )


async def _provision_stream_generator(
    req: CloudVMProvisionRequest,
    session: object,
    actor: str | None,
    gated: object,
) -> AsyncIterator[str]:
    proxmox: ProxmoxSession | None = None
    lease = None
    # True once the lease has been released OR bound to the VM. The finally block
    # releases an unsettled lease so an allocated IP is never leaked, including on
    # GeneratorExit (client disconnects while the generator is suspended at a
    # yield) which is a BaseException and bypasses the except clauses below.
    lease_settled = False

    async def _settle_lease_release() -> None:
        nonlocal lease_settled
        if lease is not None and not lease_settled:
            await _release_cloud_network_lease(lease)
        lease_settled = True

    try:
        req, lease = await _prepare_qemu_cloud_network_request(req, session)
        yield sse_event(
            "provision_step",
            {
                "step": "open_session",
                "label": "Open Proxmox session",
                "status": "started",
            },
        )
        try:
            proxmox = await _open_proxmox_session(gated)
        except Exception as error:  # noqa: BLE001
            await _settle_lease_release()
            safe = scrub_cloud_init({"e": str(error)})["e"]
            yield sse_event(
                "provision_step",
                {
                    "step": "open_session",
                    "label": "Open Proxmox session",
                    "status": "error",
                    "error": safe,
                },
            )
            yield sse_event("complete", {"ok": False, "error": safe, "step": "open_session"})
            return

        yield sse_event(
            "provision_step",
            {
                "step": "open_session",
                "label": "Open Proxmox session",
                "status": "done",
            },
        )

        stream_state = _StreamProvisionState()
        for emit_phase in (
            _emit_clone_template_events,
            _emit_configure_cloud_init_events,
            _emit_resize_disk_events,
            _emit_start_vm_events,
        ):
            async for event in emit_phase(proxmox, req, stream_state):
                yield event
            if stream_state.failed:
                await _settle_lease_release()
                return

        vm_status = "started" if req.start_after_provision else "stopped"
        yield sse_event(
            "terminal_line", {"line": f"✓ VM {req.new_vmid} provisioned · status={vm_status}"}
        )

        provision_response = CloudVMProvisionResponse(
            new_vmid=req.new_vmid,
            clone_upid=stream_state.clone_upid,
            config_upid=stream_state.config_upid,
            resize_upid=stream_state.resize_upid,
            start_upid=stream_state.start_upid,
            status=vm_status,
        )
        await _journal_provision_best_effort(
            session=session,
            proxmox=proxmox,
            endpoint=gated,
            request=req,
            response=provision_response,
            actor=actor,
        )
        await _bind_allocated_ip_to_vm_best_effort(req=req, lease=lease)
        lease_settled = True  # IP now belongs to the VM; do not release it

        yield sse_event(
            "complete",
            {
                "ok": True,
                "new_vmid": req.new_vmid,
                "clone_upid": stream_state.clone_upid,
                "config_upid": stream_state.config_upid,
                "resize_upid": stream_state.resize_upid,
                "start_upid": stream_state.start_upid,
                "status": vm_status,
            },
        )

    except asyncio.CancelledError:
        logger.info("cloud provision stream cancelled for vmid=%s", req.new_vmid)
        await _settle_lease_release()
        yield sse_event("complete", {"ok": False, "error": "Stream cancelled."})
    except Exception as error:  # noqa: BLE001
        await _settle_lease_release()
        safe = scrub_cloud_init({"e": str(error)})["e"]
        logger.warning("cloud provision stream unexpected error vmid=%s: %s", req.new_vmid, safe)
        yield sse_event("complete", {"ok": False, "error": safe})
    finally:
        await _settle_lease_release()
        if proxmox is not None:
            await proxmox.aclose()


@stream_router.post("/vm/provision/stream", response_model=None)
async def provision_vm_stream(
    req: CloudVMProvisionRequest,
    session: SessionDep,
    actor: Annotated[str | None, Header(alias="X-Proxbox-Actor")] = None,
) -> StreamingResponse | JSONResponse:
    gated = await _cloud_provision_gate(session, req.endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated
    if req.enforce_cloud_network:
        _require_resolved_cloud_network()

    return StreamingResponse(
        _provision_stream_generator(req, session, actor, gated),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
