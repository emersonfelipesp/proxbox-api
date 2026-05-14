"""Cloud Portal VM provisioning route."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.exception import ProxboxException, ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.routes.intent.cloud_init import build_proxmox_ci_args
from proxbox_api.routes.intent.dispatchers.common import mapping_from_response
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.schemas.cloud_provision import (
    CloudVMProvisionRequest,
    CloudVMProvisionResponse,
)
from proxbox_api.services.proxmox_helpers import get_node_task_status
from proxbox_api.services.verb_dispatch import (
    resolve_netbox_vm_id,
    resolve_proxmox_node,
    utcnow_iso,
    write_verb_journal_entry,
)
from proxbox_api.session.netbox import get_netbox_async_session
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.utils.async_compat import maybe_await as _maybe_await
from proxbox_api.utils.log_scrubbing import scrub_cloud_init

router = APIRouter()

_TASK_TIMEOUT_SECONDS = 300.0
_TASK_POLL_INTERVAL_SECONDS = 1.0
_DRIVE_PREFIXES = ("ide", "sata", "scsi", "virtio")


def _extract_task_id(response: object) -> str | None:
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        data = response.get("data")
        if isinstance(data, str):
            return data
        if isinstance(data, Mapping):
            task_id = data.get("upid") or data.get("UPID")
            return str(task_id) if task_id is not None else None
    return None


def _is_upid(value: str | None) -> bool:
    return bool(value and value.startswith("UPID:"))


async def _wait_for_upid(proxmox: ProxmoxSession, node: str, upid: str) -> None:
    async def _poll() -> None:
        while True:
            task_status = await get_node_task_status(proxmox, node, upid)
            status_value = getattr(task_status, "status", None)
            exitstatus = getattr(task_status, "exitstatus", None)
            if status_value == "stopped":
                if exitstatus in (None, "OK"):
                    return
                raise ProxmoxAPIError(message=f"Proxmox task failed with exitstatus={exitstatus}")
            await asyncio.sleep(_TASK_POLL_INTERVAL_SECONDS)

    await asyncio.wait_for(_poll(), timeout=_TASK_TIMEOUT_SECONDS)


def _has_cloudinit_drive(config_payload: object) -> bool:
    config = mapping_from_response(config_payload)
    for key, value in config.items():
        if not any(key.startswith(prefix) for prefix in _DRIVE_PREFIXES):
            continue
        if "cloudinit" in str(value).lower():
            return True
    return False


def _pick_unused_ide_slot(config_payload: object | None) -> str | None:
    if config_payload is None:
        return "ide2"
    config = mapping_from_response(config_payload)
    for slot in ("ide2", "ide0", "ide1", "ide3"):
        if slot not in config:
            return slot
    return None


def _infer_default_storage(config_payload: object | None) -> str:
    if config_payload is not None:
        for value in mapping_from_response(config_payload).values():
            if not isinstance(value, str) or ":" not in value:
                continue
            storage, _sep, _volume = value.partition(":")
            if storage and "cloudinit" not in value.lower():
                return storage
    return "local-lvm"


def _proxmox_step_failed(step: str, error: Exception) -> HTTPException:
    safe_error = scrub_cloud_init({"error": str(error)}).get("error", str(error))
    logger.warning("cloud provision failed at step=%s: %s", step, safe_error)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "reason": "proxmox_step_failed",
            "step": step,
            "error": safe_error,
        },
    )


async def _get_qemu_config_best_effort(
    proxmox: ProxmoxSession,
    *,
    node: str,
    vmid: int,
) -> object | None:
    try:
        return await _maybe_await(proxmox.session.nodes(node).qemu(vmid).config.get())
    except Exception as error:  # noqa: BLE001
        logger.info(
            "cloud provision could not read qemu config for vmid=%s node=%s; adding cloudinit drive defensively: %s",
            vmid,
            node,
            error,
        )
        return None


async def _journal_provision(
    *,
    session: object,
    proxmox: ProxmoxSession,
    endpoint: object,
    request: CloudVMProvisionRequest,
    response: CloudVMProvisionResponse,
    actor: str | None,
) -> None:
    try:
        nb = await get_netbox_async_session(database_session=session)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "cloud provision: NetBox session unavailable while journaling vmid=%s: %s",
            request.new_vmid,
            scrub_cloud_init({"error": str(error)}).get("error", str(error)),
        )
        return

    try:
        netbox_vm_id = await resolve_netbox_vm_id(nb, request.new_vmid)
    except ProxboxException as error:
        logger.warning(
            "cloud provision: failed to resolve NetBox VM for vmid=%s: %s",
            request.new_vmid,
            error,
        )
        return
    if netbox_vm_id is None:
        logger.info(
            "cloud provision: no NetBox VM found for vmid=%s; skipping journal entry",
            request.new_vmid,
        )
        return

    resolved_node = await resolve_proxmox_node(proxmox, "qemu", request.new_vmid)
    node = resolved_node if isinstance(resolved_node, str) else request.target_node
    comments = "\n".join(
        [
            "Proxbox operational verb dispatched.",
            "",
            "- verb: cloud_init_provision",
            f"- actor: {actor or 'proxbox-api'}",
            f"- result: {response.status}",
            f"- endpoint: {getattr(endpoint, 'name', 'unknown')} (id={getattr(endpoint, 'id', 0)})",
            f"- dispatched_at: {utcnow_iso()}",
            f"- target_vmid: {request.new_vmid}",
            f"- template_vmid: {request.template_vmid}",
            f"- target_node: {node}",
            f"- clone_upid: {response.clone_upid or ''}",
            f"- config_upid: {response.config_upid or ''}",
            f"- start_upid: {response.start_upid or ''}",
        ]
    )
    await write_verb_journal_entry(
        nb,
        netbox_vm_id=netbox_vm_id,
        kind="success" if response.status == "started" else "info",
        comments=comments,
    )


async def _clone_template_vm(proxmox: ProxmoxSession, req: CloudVMProvisionRequest) -> str | None:
    template_node = await resolve_proxmox_node(proxmox, "qemu", req.template_vmid)
    if not isinstance(template_node, str) or not template_node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "template_vmid_not_found",
                "template_vmid": req.template_vmid,
            },
        )
    clone_payload: dict[str, object] = {
        "newid": req.new_vmid,
        "name": req.new_name,
        "target": req.target_node,
        "full": 1 if req.full_clone else 0,
    }
    if req.storage:
        clone_payload["storage"] = req.storage
    clone_result = await _maybe_await(
        proxmox.session.nodes(template_node).qemu(req.template_vmid).clone.post(**clone_payload)
    )
    clone_upid = _extract_task_id(clone_result)
    if _is_upid(clone_upid):
        await _wait_for_upid(proxmox, template_node, clone_upid)
    return clone_upid


async def _configure_cloud_init_vm(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
) -> str | None:
    existing_config = await _get_qemu_config_best_effort(
        proxmox,
        node=req.target_node,
        vmid=req.new_vmid,
    )
    ci_args = build_proxmox_ci_args(req.cloud_init)
    if req.memory_mb is not None:
        ci_args["memory"] = req.memory_mb
    if req.cores is not None:
        ci_args["cores"] = req.cores
    if existing_config is None or not _has_cloudinit_drive(existing_config):
        slot = _pick_unused_ide_slot(existing_config)
        if slot is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "reason": "no_unused_ide_slot",
                    "vmid": req.new_vmid,
                },
            )
        storage = req.storage or _infer_default_storage(existing_config)
        ci_args[slot] = f"{storage}:cloudinit"

    config_result = await _maybe_await(
        proxmox.session.nodes(req.target_node).qemu(req.new_vmid).config.put(**ci_args)
    )
    return _extract_task_id(config_result)


async def _start_vm_after_provision(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
) -> str | None:
    if not req.start_after_provision:
        return None
    start_result = await _maybe_await(
        proxmox.session.nodes(req.target_node).qemu(req.new_vmid).status.start.post()
    )
    return _extract_task_id(start_result)


@router.post("/vm/provision", response_model=CloudVMProvisionResponse)
async def provision_vm(
    req: CloudVMProvisionRequest,
    session: SessionDep,
    actor: Annotated[str | None, Header(alias="X-Proxbox-Actor")] = None,
) -> CloudVMProvisionResponse | JSONResponse:
    gated = await _gate(session, req.endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated

    proxmox: ProxmoxSession | None = None
    try:
        try:
            proxmox = await _open_proxmox_session(gated)
        except Exception as error:  # noqa: BLE001
            raise _proxmox_step_failed("open_session", error) from error

        try:
            clone_upid = await _clone_template_vm(proxmox, req)
        except Exception as error:  # noqa: BLE001
            raise _proxmox_step_failed("clone", error) from error

        try:
            config_upid = await _configure_cloud_init_vm(proxmox, req)
        except Exception as error:  # noqa: BLE001
            raise _proxmox_step_failed("configure_cloud_init", error) from error

        try:
            start_upid = await _start_vm_after_provision(proxmox, req)
        except Exception as error:  # noqa: BLE001
            raise _proxmox_step_failed("start", error) from error

        response = CloudVMProvisionResponse(
            new_vmid=req.new_vmid,
            clone_upid=clone_upid,
            config_upid=config_upid,
            start_upid=start_upid,
            status="started" if req.start_after_provision else "stopped",
        )
        await _journal_provision(
            session=session,
            proxmox=proxmox,
            endpoint=gated,
            request=req,
            response=response,
            actor=actor,
        )
        return response
    finally:
        if proxmox is not None:
            await proxmox.aclose()
