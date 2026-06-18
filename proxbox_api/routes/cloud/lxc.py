"""LXC container cloud provisioning and CT-template listing routes.

Closes https://git.nmulti.cloud/emersonfelipesp/proxbox-api/issues/90
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.exception import ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.services.proxmox_helpers import get_node_storage_content, get_node_task_status
from proxbox_api.session.proxmox import ProxmoxSession

router = APIRouter()

_TASK_TIMEOUT_SECONDS = 300.0
_TASK_POLL_INTERVAL_SECONDS = 1.0


class CloudLXCTemplateItem(BaseModel):
    volid: str
    storage: str
    path: str | None = None
    size: int | None = None


class CloudLXCProvisionRequest(BaseModel):
    endpoint_id: int = Field(ge=1)
    hostname: str = Field(min_length=1, max_length=63)
    ostemplate: str = Field(min_length=1, max_length=512)
    target_node: str = Field(min_length=1, max_length=128)
    rootfs_storage: str = Field(default="local-lvm", max_length=64)
    rootfs_size_gb: int = Field(default=8, ge=1, le=10000)
    memory_mb: int | None = Field(default=None, ge=64)
    cores: int | None = Field(default=None, ge=1)
    password: str | None = Field(default=None, max_length=256)
    start_after_provision: bool = True


class CloudLXCProvisionResponse(BaseModel):
    new_vmid: int
    create_upid: str | None = None
    start_upid: str | None = None
    status: str


def _extract_upid(response: object) -> str | None:
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


async def _wait_for_upid(proxmox: ProxmoxSession, node: str, upid: str) -> None:
    async def _poll() -> None:
        while True:
            task_status = await get_node_task_status(proxmox, node, upid)
            status_value = getattr(task_status, "status", None)
            exitstatus = getattr(task_status, "exitstatus", None)
            if status_value == "stopped":
                if exitstatus in (None, "OK"):
                    return
                raise ProxmoxAPIError(
                    message=f"Proxmox LXC create task failed: exitstatus={exitstatus}"
                )
            await asyncio.sleep(_TASK_POLL_INTERVAL_SECONDS)

    await asyncio.wait_for(_poll(), timeout=_TASK_TIMEOUT_SECONDS)


def _is_mock_mode() -> bool:
    return os.getenv("PROXMOX_API_MODE") == "mock"


async def _get_next_vmid(proxmox: ProxmoxSession) -> int:
    """Reserve the next available VMID from the Proxmox cluster."""
    try:
        result = await resolve_async(proxmox.session.cluster.nextid.get())
    except Exception as error:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "nextid_failed", "error": str(error)},
        ) from error

    if isinstance(result, (int, str)):
        try:
            return int(result)
        except (ValueError, TypeError):
            pass
    if isinstance(result, Mapping):
        data = result.get("data")
        if data is not None:
            try:
                return int(data)
            except (ValueError, TypeError):
                pass
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Could not parse next VMID from Proxmox cluster.",
    )


def _coerce_list(raw: object) -> list[object]:
    if isinstance(raw, Mapping):
        inner = raw.get("data")
        if isinstance(inner, Sequence) and not isinstance(inner, str):
            return list(inner)
        return []
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        return list(raw)
    return []


@router.get("/lxc/templates", response_model=list[CloudLXCTemplateItem])
async def list_lxc_templates(
    session: SessionDep,
    endpoint_id: int = Query(ge=1),
) -> list[CloudLXCTemplateItem] | JSONResponse:
    """List available LXC CT templates from all storages on the given Proxmox endpoint."""
    gated = await _gate(session, endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated

    proxmox: ProxmoxSession | None = None
    try:
        proxmox = await _open_proxmox_session(gated)

        nodes_raw = await resolve_async(proxmox.session.nodes.get())
        node_names: list[str] = []
        for item in _coerce_list(nodes_raw):
            if not isinstance(item, Mapping):
                continue
            node_status = item.get("status", "online")
            node_name = item.get("node")
            if node_name and node_status in ("online", ""):
                node_names.append(str(node_name))

        if not node_names:
            return []

        # Use the first online node to discover storages with vztmpl content
        node = node_names[0]

        try:
            storages_raw = await resolve_async(
                proxmox.session.nodes(node).storage.get(content="vztmpl")
            )
        except Exception as err:  # noqa: BLE001
            logger.info("lxc templates: could not list storages on node=%s: %s", node, err)
            return []

        storage_names: list[str] = []
        for s in _coerce_list(storages_raw):
            if isinstance(s, Mapping):
                sname = s.get("storage")
                if sname:
                    storage_names.append(str(sname))

        templates: list[CloudLXCTemplateItem] = []
        seen_volids: set[str] = set()
        for storage_name in storage_names:
            try:
                items = await get_node_storage_content(
                    proxmox, node, storage_name, content="vztmpl"
                )
                for item in items or []:
                    volid = (
                        getattr(item, "volid", None)
                        or (isinstance(item, Mapping) and item.get("volid"))
                        or None
                    )
                    if not volid or volid in seen_volids:
                        continue
                    seen_volids.add(str(volid))
                    templates.append(
                        CloudLXCTemplateItem(
                            volid=str(volid),
                            storage=storage_name,
                            path=str(
                                getattr(item, "path", None)
                                or (isinstance(item, Mapping) and item.get("path"))
                                or ""
                            )
                            or None,
                            size=(
                                getattr(item, "size", None)
                                or (isinstance(item, Mapping) and item.get("size"))
                                or None
                            ),
                        )
                    )
            except Exception as err:  # noqa: BLE001
                logger.info(
                    "lxc templates: skipping storage %s/%s: %s", node, storage_name, err
                )

        return templates
    finally:
        if proxmox is not None:
            await proxmox.aclose()


@router.post(
    "/lxc/provision",
    response_model=CloudLXCProvisionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def provision_lxc(
    req: CloudLXCProvisionRequest,
    session: SessionDep,
    actor: Annotated[str | None, Header(alias="X-Proxbox-Actor")] = None,
) -> CloudLXCProvisionResponse | JSONResponse:
    """Create an LXC container on the given Proxmox endpoint.

    Auto-allocates a VMID via Proxmox ``/cluster/nextid``.  The caller
    provides the hostname, ostemplate path, rootfs storage/size, and compute
    resources; the endpoint's ``allow_writes`` flag must be enabled.
    """
    gated = await _gate(session, req.endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated

    proxmox: ProxmoxSession | None = None
    try:
        proxmox = await _open_proxmox_session(gated)
        new_vmid = await _get_next_vmid(proxmox)

        # Build creation params — password scrubbed from debug log, restored for actual call
        params: dict[str, object] = {
            "vmid": new_vmid,
            "hostname": req.hostname,
            "ostemplate": req.ostemplate,
            "rootfs": f"{req.rootfs_storage}:{req.rootfs_size_gb}",
        }
        if req.memory_mb is not None:
            params["memory"] = req.memory_mb
        if req.cores is not None:
            params["cores"] = req.cores
        if req.password is not None:
            params["password"] = req.password

        logger.debug(
            "lxc provision: endpoint=%s vmid=%s node=%s ostemplate=%s rootfs=%s",
            req.endpoint_id,
            new_vmid,
            req.target_node,
            req.ostemplate,
            params["rootfs"],
        )

        create_result = await resolve_async(
            proxmox.session.nodes(req.target_node).lxc.post(**params)
        )
        create_upid = _extract_upid(create_result)

        if create_upid and create_upid.startswith("UPID:") and not _is_mock_mode():
            await _wait_for_upid(proxmox, req.target_node, create_upid)

        start_upid: str | None = None
        if req.start_after_provision:
            try:
                start_result = await resolve_async(
                    proxmox.session.nodes(req.target_node).lxc(new_vmid).status.start.post()
                )
                start_upid = _extract_upid(start_result)
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    "lxc provision: container vmid=%s created but start failed: %s",
                    new_vmid,
                    err,
                )

        logger.info(
            "lxc provision: success endpoint=%s vmid=%s node=%s actor=%s",
            req.endpoint_id,
            new_vmid,
            req.target_node,
            actor or "proxbox-api",
        )
        return CloudLXCProvisionResponse(
            new_vmid=new_vmid,
            create_upid=create_upid,
            start_upid=start_upid,
            status="started" if req.start_after_provision else "stopped",
        )
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.warning("lxc provision failed endpoint=%s: %s", req.endpoint_id, error)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "lxc_provision_failed", "error": str(error)},
        ) from error
    finally:
        if proxmox is not None:
            await proxmox.aclose()
