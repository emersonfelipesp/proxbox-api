"""LXC container cloud provisioning and CT-template listing routes.

Closes https://git.nmulti.cloud/emersonfelipesp/proxbox-api/issues/90
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.exception import ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.routes.cloud.qemu_templates import _endpoint_for_read
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.services.cloud_network import (
    AllocatedIPAddress,
    CloudNetworkConfig,
    allocate_ip,
    release_ip,
    resolve_cloud_network,
    validate_cloud_network_configured,
)
from proxbox_api.services.proxmox_helpers import get_node_storage_content, get_node_task_status
from proxbox_api.session.netbox import get_netbox_async_session
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.utils.log_scrubbing import scrub_cloud_init

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
    enforce_cloud_network: bool = False


class CloudLXCProvisionResponse(BaseModel):
    new_vmid: int
    create_upid: str | None = None
    start_upid: str | None = None
    status: str


@dataclass(frozen=True, slots=True)
class _CloudNetworkLease:
    ip_id: int | None
    netbox_session: object


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


def _online_node_names(nodes_raw: object) -> list[str]:
    node_names: list[str] = []
    for item in _coerce_list(nodes_raw):
        if not isinstance(item, Mapping):
            continue
        node_status = item.get("status", "online")
        node_name = item.get("node")
        if node_name and node_status in ("online", ""):
            node_names.append(str(node_name))
    return node_names


def _storage_names(storages_raw: object) -> list[str]:
    names: list[str] = []
    for item in _coerce_list(storages_raw):
        if not isinstance(item, Mapping):
            continue
        storage_name = item.get("storage")
        if storage_name:
            names.append(str(storage_name))
    return names


def _item_value(item: object, key: str) -> object | None:
    value = getattr(item, key, None)
    if value is not None:
        return value
    if isinstance(item, Mapping):
        return item.get(key)
    return None


def _template_item(
    item: object,
    *,
    storage_name: str,
    seen_volids: set[str],
) -> CloudLXCTemplateItem | None:
    volid = _item_value(item, "volid")
    if not volid:
        return None
    volid_str = str(volid)
    if volid_str in seen_volids:
        return None
    seen_volids.add(volid_str)

    path = _item_value(item, "path")
    size = _item_value(item, "size")
    return CloudLXCTemplateItem(
        volid=volid_str,
        storage=storage_name,
        path=str(path) if path else None,
        size=size,
    )


async def _list_vztmpl_storages(
    proxmox: ProxmoxSession,
    node: str,
) -> list[str]:
    try:
        storages_raw = await resolve_async(
            proxmox.session.nodes(node).storage.get(content="vztmpl")
        )
    except Exception as err:  # noqa: BLE001
        logger.info("lxc templates: could not list storages on node=%s: %s", node, err)
        return []
    return _storage_names(storages_raw)


async def _list_storage_templates(
    proxmox: ProxmoxSession,
    node: str,
    storage_name: str,
    seen_volids: set[str],
) -> list[CloudLXCTemplateItem]:
    try:
        items = await get_node_storage_content(proxmox, node, storage_name, content="vztmpl")
    except Exception as err:  # noqa: BLE001
        logger.info("lxc templates: skipping storage %s/%s: %s", node, storage_name, err)
        return []

    templates: list[CloudLXCTemplateItem] = []
    for item in items or []:
        template = _template_item(item, storage_name=storage_name, seen_volids=seen_volids)
        if template is not None:
            templates.append(template)
    return templates


def _build_lxc_create_params(
    req: CloudLXCProvisionRequest,
    new_vmid: int,
    *,
    cloud_network: CloudNetworkConfig | None = None,
    allocated_ip: AllocatedIPAddress | None = None,
) -> dict[str, object]:
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
    if cloud_network is not None and allocated_ip is not None:
        net0 = [
            "name=eth0",
            f"bridge={cloud_network.bridge}",
        ]
        if cloud_network.vlan_tag is not None:
            net0.append(f"tag={cloud_network.vlan_tag}")
        net0.extend([f"ip={allocated_ip.cidr}", f"gw={cloud_network.gateway}"])
        params["net0"] = ",".join(net0)
    return params


def _cloud_network_http_exception(error: ValueError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=str(error),
    )


async def _prepare_lxc_cloud_network(
    req: CloudLXCProvisionRequest,
    session: object,
) -> tuple[CloudNetworkConfig | None, AllocatedIPAddress | None, _CloudNetworkLease | None]:
    if not req.enforce_cloud_network:
        return None, None, None

    cloud_network = resolve_cloud_network()
    try:
        validate_cloud_network_configured(cloud_network)
    except ValueError as error:
        raise _cloud_network_http_exception(error) from error

    nb = await get_netbox_async_session(database_session=session)
    if cloud_network.prefix_id is None:
        raise _cloud_network_http_exception(ValueError("cloud network not configured"))
    allocated_ip = await allocate_ip(cloud_network.prefix_id, netbox_session=nb)
    return (
        cloud_network,
        allocated_ip,
        _CloudNetworkLease(
            ip_id=allocated_ip.id,
            netbox_session=nb,
        ),
    )


async def _release_lxc_cloud_network_lease(lease: _CloudNetworkLease | None) -> None:
    if lease is None or lease.ip_id is None:
        return
    await release_ip(lease.ip_id, netbox_session=lease.netbox_session)


async def _create_lxc_container(
    proxmox: ProxmoxSession,
    req: CloudLXCProvisionRequest,
    params: dict[str, object],
) -> str | None:
    create_result = await resolve_async(proxmox.session.nodes(req.target_node).lxc.post(**params))
    create_upid = _extract_upid(create_result)
    if create_upid and create_upid.startswith("UPID:") and not _is_mock_mode():
        await _wait_for_upid(proxmox, req.target_node, create_upid)
    return create_upid


async def _start_lxc_container(
    proxmox: ProxmoxSession,
    req: CloudLXCProvisionRequest,
    new_vmid: int,
) -> str | None:
    if not req.start_after_provision:
        return None
    try:
        start_result = await resolve_async(
            proxmox.session.nodes(req.target_node).lxc(new_vmid).status.start.post()
        )
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "lxc provision: container vmid=%s created but start failed: %s",
            new_vmid,
            err,
        )
        return None
    return _extract_upid(start_result)


async def _provision_lxc_on_proxmox(
    proxmox: ProxmoxSession,
    req: CloudLXCProvisionRequest,
    actor: str | None,
    *,
    cloud_network: CloudNetworkConfig | None = None,
    allocated_ip: AllocatedIPAddress | None = None,
) -> CloudLXCProvisionResponse:
    new_vmid = await _get_next_vmid(proxmox)
    params = _build_lxc_create_params(
        req,
        new_vmid,
        cloud_network=cloud_network,
        allocated_ip=allocated_ip,
    )

    logger.debug(
        "lxc provision: endpoint=%s vmid=%s node=%s ostemplate=%s rootfs=%s",
        req.endpoint_id,
        new_vmid,
        req.target_node,
        req.ostemplate,
        params["rootfs"],
    )

    create_upid = await _create_lxc_container(proxmox, req, params)
    start_upid = await _start_lxc_container(proxmox, req, new_vmid)

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


@router.get("/lxc/templates", response_model=list[CloudLXCTemplateItem])
async def list_lxc_templates(
    session: SessionDep,
    endpoint_id: int = Query(ge=1),
) -> list[CloudLXCTemplateItem]:
    """List available LXC CT templates from all storages on the given Proxmox endpoint.

    This is a read-only discovery route, so it resolves the endpoint through
    ``_endpoint_for_read`` (existence + ``enabled``) — the same read gate the
    sibling QEMU template route uses — instead of the write gate ``_gate``.
    Listing templates must NOT require ``ProxmoxEndpoint.allow_writes=True``;
    that flag only guards mutating verbs (see ``provision_lxc`` below).
    """
    endpoint = await _endpoint_for_read(session, endpoint_id)

    proxmox: ProxmoxSession | None = None
    try:
        proxmox = await _open_proxmox_session(endpoint)

        nodes_raw = await resolve_async(proxmox.session.nodes.get())
        node_names = _online_node_names(nodes_raw)
        if not node_names:
            return []

        # Use the first online node to discover storages with vztmpl content
        node = node_names[0]
        storage_names = await _list_vztmpl_storages(proxmox, node)

        templates: list[CloudLXCTemplateItem] = []
        seen_volids: set[str] = set()
        for storage_name in storage_names:
            templates.extend(
                await _list_storage_templates(proxmox, node, storage_name, seen_volids)
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
    cloud_network: CloudNetworkConfig | None = None
    allocated_ip: AllocatedIPAddress | None = None
    lease: _CloudNetworkLease | None = None
    try:
        cloud_network, allocated_ip, lease = await _prepare_lxc_cloud_network(req, session)
        proxmox = await _open_proxmox_session(gated)
        return await _provision_lxc_on_proxmox(
            proxmox,
            req,
            actor,
            cloud_network=cloud_network,
            allocated_ip=allocated_ip,
        )
    except HTTPException:
        # The NetBox IP allocation happens before Proxmox creation; release it
        # when any later validation or Proxmox step aborts the container create.
        await _release_lxc_cloud_network_lease(lease)
        raise
    except Exception as error:  # noqa: BLE001
        # Best-effort rollback keeps a failed LXC create from consuming a
        # customer-network IP when Proxmox rejects the request after allocation.
        await _release_lxc_cloud_network_lease(lease)
        # Scrub the cloud-init password (Proxmox `password`) out of the error
        # text before it reaches the log or the client 502 body — parity with
        # the QEMU provision path (the LXC create carries `password` too).
        safe_error = scrub_cloud_init({"error": str(error)}).get("error", str(error))
        logger.warning("lxc provision failed endpoint=%s: %s", req.endpoint_id, safe_error)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "lxc_provision_failed", "error": safe_error},
        ) from error
    finally:
        if proxmox is not None:
            await proxmox.aclose()
