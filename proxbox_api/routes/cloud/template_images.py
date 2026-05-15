"""Cloud image template build route."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.routes.cloud.provision import _extract_task_id, _wait_for_upid
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.schemas.cloud_provision import (
    CloudImageTemplateBuildRequest,
    CloudImageTemplateBuildResponse,
)
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.ssrf import validate_endpoint_url
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()

_IMAGE_EXTENSIONS = {".qcow2", ".raw", ".vmdk", ".vma"}


def _filename_from_request(req: CloudImageTemplateBuildRequest) -> str:
    raw = (req.image_filename or "").strip()
    if not raw:
        raw = PurePosixPath(urlsplit(req.image_url).path).name
    if not raw:
        raise HTTPException(status_code=422, detail="image_filename could not be derived.")
    path = PurePosixPath(raw)
    suffix = path.suffix.lower()
    if suffix == ".img":
        return f"{path.stem}.qcow2"
    if suffix not in _IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"image_filename must end with one of {sorted(_IMAGE_EXTENSIONS)}.",
        )
    return path.name


def _is_ready_template(config: object) -> bool:
    if not isinstance(config, dict):
        return False
    return (
        str(config.get("template")) in {"1", "True", "true"}
        and "scsi0" in config
        and "cloudinit" in str(config.get("ide2", "")).lower()
        and "scsi0" in str(config.get("boot", ""))
    )


async def _vm_config_or_none(
    proxmox: ProxmoxSession,
    *,
    node: str,
    vmid: int,
) -> dict[str, Any] | None:
    try:
        config = await _maybe_await(proxmox.session.nodes(node).qemu(vmid).config.get())
    except Exception:  # noqa: BLE001
        return None
    return config if isinstance(config, dict) else {}


async def _image_exists(
    proxmox: ProxmoxSession,
    *,
    node: str,
    storage: str,
    volid: str,
) -> bool:
    try:
        content = await _maybe_await(
            proxmox.session.nodes(node).storage(storage).content.get(content="import")
        )
    except Exception:  # noqa: BLE001
        return False
    rows = content if isinstance(content, list) else []
    return any(isinstance(row, dict) and row.get("volid") == volid for row in rows)


async def _wait_for_task(
    proxmox: ProxmoxSession,
    *,
    node: str,
    response: object,
) -> str | None:
    upid = _extract_task_id(response)
    if upid and upid.startswith("UPID:"):
        await _wait_for_upid(proxmox, node, upid)
    return upid


@router.post(
    "/templates/images",
    response_model=CloudImageTemplateBuildResponse,
    status_code=status.HTTP_201_CREATED,
)
async def build_cloud_image_template(
    req: CloudImageTemplateBuildRequest,
    session: SessionDep,
) -> CloudImageTemplateBuildResponse | JSONResponse:
    """Create a bootable Proxmox template from a cloud image URL."""
    gated = await _gate(session, req.endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated

    filename = _filename_from_request(req)
    image_volid = f"{req.image_storage}:import/{filename}"

    safe, reason = validate_endpoint_url(req.image_url)
    if not safe:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"image_url rejected by SSRF protection: {reason}",
        )

    proxmox: ProxmoxSession | None = None
    try:
        proxmox = await _open_proxmox_session(gated)
        existing = await _vm_config_or_none(
            proxmox,
            node=req.target_node,
            vmid=req.vmid,
        )
        if existing is not None:
            if _is_ready_template(existing):
                return CloudImageTemplateBuildResponse(
                    endpoint_id=req.endpoint_id,
                    target_node=req.target_node,
                    vmid=req.vmid,
                    name=str(existing.get("name") or req.name),
                    status="already_exists",
                    image_volid=image_volid,
                    boot=existing.get("boot"),
                    scsi0=existing.get("scsi0"),
                    ide2=existing.get("ide2"),
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"VMID {req.vmid} already exists and is not a ready cloud-init template.",
            )

        download_upid = None
        if not await _image_exists(
            proxmox,
            node=req.target_node,
            storage=req.image_storage,
            volid=image_volid,
        ):
            download_result = await _maybe_await(
                proxmox.session.nodes(req.target_node)
                .storage(f"{req.image_storage}/download-url")
                .post(
                    content="import",
                    filename=filename,
                    url=req.image_url,
                    **{"verify-certificates": 1 if req.verify_image_certificates else 0},
                )
            )
            download_upid = await _wait_for_task(
                proxmox,
                node=req.target_node,
                response=download_result,
            )

        create_kwargs: dict[str, object] = {
            "vmid": req.vmid,
            "name": req.name,
            "memory": req.memory_mb,
            "cores": req.cores,
            "sockets": 1,
            "ostype": req.os_type,
            "agent": "enabled=1",
            "scsihw": "virtio-scsi-pci",
            "scsi0": f"{req.vm_storage}:0,import-from={image_volid},discard=on",
            "ide2": f"{req.vm_storage}:cloudinit",
            "boot": "order=scsi0",
            "serial0": "socket",
            "vga": "serial0",
            "net0": f"virtio,bridge={req.bridge}",
            "ciuser": req.ciuser,
            "ipconfig0": "ip=dhcp",
        }
        if req.cpu:
            create_kwargs["cpu"] = req.cpu
        if req.description:
            create_kwargs["description"] = req.description
        create_result = await _maybe_await(
            proxmox.session.nodes(req.target_node).qemu.post(**create_kwargs)
        )
        create_upid = await _wait_for_task(proxmox, node=req.target_node, response=create_result)

        template_result = await _maybe_await(
            proxmox.session.nodes(req.target_node).qemu(req.vmid).template.post(disk="scsi0")
        )
        template_upid = await _wait_for_task(
            proxmox,
            node=req.target_node,
            response=template_result,
        )
        config = await _vm_config_or_none(proxmox, node=req.target_node, vmid=req.vmid)
        if not _is_ready_template(config):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Proxmox template was created but did not reach the expected config.",
            )
        config = config or {}
        return CloudImageTemplateBuildResponse(
            endpoint_id=req.endpoint_id,
            target_node=req.target_node,
            vmid=req.vmid,
            name=str(config.get("name") or req.name),
            status="created",
            image_volid=image_volid,
            download_upid=download_upid,
            create_upid=create_upid,
            template_upid=template_upid,
            boot=config.get("boot"),
            scsi0=config.get("scsi0"),
            ide2=config.get("ide2"),
        )
    finally:
        if proxmox is not None:
            await proxmox.aclose()
