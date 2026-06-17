"""Cloud Portal VM provisioning SSE stream route."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, StreamingResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.logger import logger
from proxbox_api.routes.cloud.provision import (
    _clone_template_vm,
    _cloud_provision_gate,
    _configure_cloud_init_vm,
    _journal_provision_best_effort,
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
    ("start_vm", "Start virtual machine"),
]


async def _provision_stream_generator(
    req: CloudVMProvisionRequest,
    session: object,
    actor: str | None,
    gated: object,
) -> AsyncIterator[str]:
    proxmox: ProxmoxSession | None = None
    try:
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
        try:
            clone_upid = await _clone_template_vm(proxmox, req)
        except Exception as error:  # noqa: BLE001
            safe = scrub_cloud_init({"e": str(error)})["e"]
            yield sse_event(
                "provision_step",
                {
                    "step": "clone_template",
                    "label": "Clone template VM",
                    "status": "error",
                    "error": safe,
                },
            )
            yield sse_event("complete", {"ok": False, "error": safe, "step": "clone_template"})
            return

        yield sse_event("terminal_line", {"line": f"Clone task: {clone_upid or 'ok'}"})
        yield sse_event(
            "provision_step",
            {
                "step": "clone_template",
                "label": "Clone template VM",
                "status": "done",
                "upid": clone_upid,
            },
        )

        yield sse_event("terminal_line", {"line": "Applying cloud-init configuration…"})
        yield sse_event(
            "provision_step",
            {
                "step": "configure_cloud_init",
                "label": "Configure cloud-init",
                "status": "started",
            },
        )
        try:
            config_upid = await _configure_cloud_init_vm(proxmox, req)
        except Exception as error:  # noqa: BLE001
            safe = scrub_cloud_init({"e": str(error)})["e"]
            yield sse_event(
                "provision_step",
                {
                    "step": "configure_cloud_init",
                    "label": "Configure cloud-init",
                    "status": "error",
                    "error": safe,
                },
            )
            yield sse_event(
                "complete", {"ok": False, "error": safe, "step": "configure_cloud_init"}
            )
            return

        yield sse_event("terminal_line", {"line": f"Config task: {config_upid or 'ok'}"})
        yield sse_event(
            "provision_step",
            {
                "step": "configure_cloud_init",
                "label": "Configure cloud-init",
                "status": "done",
                "upid": config_upid,
            },
        )

        start_upid: str | None = None
        if req.start_after_provision:
            yield sse_event("terminal_line", {"line": f"Starting VM {req.new_vmid}…"})
            yield sse_event(
                "provision_step",
                {
                    "step": "start_vm",
                    "label": "Start virtual machine",
                    "status": "started",
                },
            )
            try:
                start_upid = await _start_vm_after_provision(proxmox, req)
            except Exception as error:  # noqa: BLE001
                safe = scrub_cloud_init({"e": str(error)})["e"]
                yield sse_event(
                    "provision_step",
                    {
                        "step": "start_vm",
                        "label": "Start virtual machine",
                        "status": "error",
                        "error": safe,
                    },
                )
                yield sse_event("complete", {"ok": False, "error": safe, "step": "start_vm"})
                return

            yield sse_event("terminal_line", {"line": f"Start task: {start_upid or 'ok'}"})
            yield sse_event(
                "provision_step",
                {
                    "step": "start_vm",
                    "label": "Start virtual machine",
                    "status": "done",
                    "upid": start_upid,
                },
            )
        else:
            yield sse_event(
                "provision_step",
                {
                    "step": "start_vm",
                    "label": "Start virtual machine",
                    "status": "skipped",
                },
            )

        vm_status = "started" if req.start_after_provision else "stopped"
        yield sse_event(
            "terminal_line", {"line": f"✓ VM {req.new_vmid} provisioned · status={vm_status}"}
        )

        provision_response = CloudVMProvisionResponse(
            new_vmid=req.new_vmid,
            clone_upid=clone_upid,
            config_upid=config_upid,
            start_upid=start_upid,
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

        yield sse_event(
            "complete",
            {
                "ok": True,
                "new_vmid": req.new_vmid,
                "clone_upid": clone_upid,
                "config_upid": config_upid,
                "start_upid": start_upid,
                "status": vm_status,
            },
        )

    except asyncio.CancelledError:
        logger.info("cloud provision stream cancelled for vmid=%s", req.new_vmid)
        yield sse_event("complete", {"ok": False, "error": "Stream cancelled."})
    except Exception as error:  # noqa: BLE001
        safe = scrub_cloud_init({"e": str(error)})["e"]
        logger.warning("cloud provision stream unexpected error vmid=%s: %s", req.new_vmid, safe)
        yield sse_event("complete", {"ok": False, "error": safe})
    finally:
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

    return StreamingResponse(
        _provision_stream_generator(req, session, actor, gated),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
