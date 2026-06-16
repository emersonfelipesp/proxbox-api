"""Live Proxmox QEMU Cloud-Init template discovery."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.routes.intent.dispatchers.common import mapping_from_response
from proxbox_api.routes.proxmox_actions import _open_proxmox_session
from proxbox_api.schemas.cloud_provision import (
    CloudQemuTemplate,
    CloudQemuTemplateListResponse,
)
from proxbox_api.services.proxmox_helpers import get_cluster_resources
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()

_DRIVE_PREFIXES = ("ide", "sata", "scsi", "virtio")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _record_from_resource(value: object) -> dict[str, object]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python", by_alias=True, exclude_none=True)
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _cloud_init_drive_keys(config: Mapping[str, object]) -> list[str]:
    keys: list[str] = []
    for key, value in config.items():
        if not any(str(key).startswith(prefix) for prefix in _DRIVE_PREFIXES):
            continue
        if "cloudinit" in str(value).lower():
            keys.append(str(key))
    return sorted(keys)


def _has_cloud_init(config: Mapping[str, object]) -> bool:
    return bool(_cloud_init_drive_keys(config) or config.get("cicustom"))


async def _endpoint_for_read(session: SessionDep, endpoint_id: int) -> ProxmoxEndpoint:
    endpoint = await _maybe_await(session.get(ProxmoxEndpoint, endpoint_id))
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "endpoint_not_found", "endpoint_id": endpoint_id},
        )
    if not endpoint.enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "endpoint_disabled", "endpoint_id": endpoint_id},
        )
    return endpoint


async def _qemu_config(
    proxmox: ProxmoxSession,
    *,
    node: str,
    vmid: int,
) -> dict[str, object]:
    payload = await _maybe_await(proxmox.session.nodes(node).qemu(vmid).config.get())
    return mapping_from_response(payload)


def _template_from_record(
    *,
    endpoint: ProxmoxEndpoint,
    cluster_name: str | None,
    record: Mapping[str, object],
    config: Mapping[str, object],
) -> CloudQemuTemplate | None:
    vmid = _as_int(record.get("vmid"))
    node = str(record.get("node") or "")
    if vmid is None or vmid < 100 or not node:
        return None

    cloud_init_drives = _cloud_init_drive_keys(config)
    name = str(record.get("name") or config.get("name") or f"template-{vmid}")
    memory_mb = _as_int(record.get("maxmem") or record.get("mem"))
    maxdisk_bytes = _as_int(record.get("maxdisk") or record.get("disk"))
    return CloudQemuTemplate(
        id=vmid,
        endpoint_id=int(endpoint.id or 0),
        endpoint_name=endpoint.name,
        cluster_name=cluster_name,
        source_vmid=vmid,
        vmid=vmid,
        name=name,
        node=node,
        target_node=node,
        status=str(record.get("status")) if record.get("status") is not None else None,
        template=True,
        cloud_init=True,
        cloud_init_drives=cloud_init_drives,
        cicustom=str(config.get("cicustom")) if config.get("cicustom") is not None else None,
        tags=str(record.get("tags")) if record.get("tags") is not None else None,
        memory_mb=memory_mb,
        maxdisk_bytes=maxdisk_bytes,
        description=str(config.get("description"))
        if config.get("description") is not None
        else None,
    )


async def _template_from_resource(
    *,
    proxmox: ProxmoxSession,
    endpoint: ProxmoxEndpoint,
    cluster_name: str | None,
    resource: object,
    cloud_init_only: bool,
) -> CloudQemuTemplate | None:
    record = _record_from_resource(resource)
    if str(record.get("type") or "").lower() != "qemu":
        return None
    if not _as_bool(record.get("template")):
        return None
    vmid = _as_int(record.get("vmid"))
    node = str(record.get("node") or "")
    if vmid is None or not node:
        return None
    try:
        config = await _qemu_config(proxmox, node=node, vmid=vmid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cloud template discovery failed to read qemu config endpoint=%s node=%s vmid=%s: %s",
            endpoint.id,
            node,
            vmid,
            exc,
        )
        if cloud_init_only:
            return None
        config = {}
    if cloud_init_only and not _has_cloud_init(config):
        return None
    return _template_from_record(
        endpoint=endpoint,
        cluster_name=cluster_name,
        record=record,
        config=config,
    )


async def _discover_qemu_cloud_init_templates(
    *,
    proxmox: ProxmoxSession,
    endpoint: ProxmoxEndpoint,
    cloud_init_only: bool,
) -> list[CloudQemuTemplate]:
    try:
        resources = await get_cluster_resources(proxmox, resource_type="vm")
    except ProxmoxAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "proxmox_cluster_resources_unreachable", "detail": str(exc)},
        ) from exc

    cluster_name = getattr(proxmox, "name", None) or endpoint.name
    templates: list[CloudQemuTemplate] = []
    for resource in resources:
        template = await _template_from_resource(
            proxmox=proxmox,
            endpoint=endpoint,
            cluster_name=str(cluster_name) if cluster_name else None,
            resource=resource,
            cloud_init_only=cloud_init_only,
        )
        if template is not None:
            templates.append(template)
    return sorted(templates, key=lambda item: (item.node, item.vmid))


@router.get("/vm/templates", response_model=CloudQemuTemplateListResponse)
async def qemu_cloud_init_templates(
    session: SessionDep,
    endpoint_id: Annotated[int, Query(ge=1, description="ProxmoxEndpoint primary key")],
    cloud_init_only: Annotated[
        bool,
        Query(description="Only include templates with a Cloud-Init drive or cicustom config."),
    ] = True,
) -> CloudQemuTemplateListResponse:
    """Return live QEMU VM templates usable as Cloud-Init clone sources."""

    endpoint = await _endpoint_for_read(session, endpoint_id)
    proxmox: ProxmoxSession | None = None
    try:
        proxmox = await _open_proxmox_session(endpoint)
        templates = await _discover_qemu_cloud_init_templates(
            proxmox=proxmox,
            endpoint=endpoint,
            cloud_init_only=cloud_init_only,
        )
        return CloudQemuTemplateListResponse(count=len(templates), results=templates)
    finally:
        if proxmox is not None:
            await proxmox.aclose()
