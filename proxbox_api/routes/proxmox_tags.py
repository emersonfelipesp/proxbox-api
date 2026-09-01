"""Operational Proxmox config tag mutation routes.

General-purpose tag replace (PUT) and merge (PATCH) for QEMU and LXC guests.
Reuses the tag helpers from the intent ``vm_tags`` module and the
``allow_writes`` gate from ``proxmox_actions``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.logger import logger
from proxbox_api.routes.intent.vm_tags import _get_current_tags, _set_tags
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session

router = APIRouter()

VmType = Literal["qemu", "lxc"]


class ReplaceProxmoxTagsBody(BaseModel):
    node: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class PatchProxmoxTagsBody(BaseModel):
    node: str = Field(min_length=1)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class ProxmoxTagsResponse(BaseModel):
    ok: bool = True
    vmid: int
    vm_type: VmType
    endpoint_id: int
    node: str
    tags_after: list[str]


def _normalize_tag_list(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in tags:
        tag = str(raw).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized


async def _apply_tags_mutation(
    *,
    vm_type: VmType,
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None,
    actor: str | None,
    node: str,
    tags: list[str],
) -> ProxmoxTagsResponse | JSONResponse:
    endpoint = await _gate(session, endpoint_id)
    if isinstance(endpoint, JSONResponse):
        return endpoint

    try:
        proxmox = await _open_proxmox_session(endpoint)
        updated_tags = _normalize_tag_list(tags)
        current_tags = await _get_current_tags(proxmox, vmid, node, vm_type)
        if current_tags != updated_tags:
            await _set_tags(proxmox, vmid, node, vm_type, updated_tags)
        logger.info(
            "proxmox_tags: set %s vmid=%s node=%s endpoint_id=%s actor=%s",
            vm_type,
            vmid,
            node,
            endpoint.id,
            actor or "unknown",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "proxmox_tags: failed to set %s vmid=%s node=%s: %s",
            vm_type,
            vmid,
            node,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Proxmox API call failed: {exc}",
        ) from exc

    return ProxmoxTagsResponse(
        vmid=vmid,
        vm_type=vm_type,
        endpoint_id=int(endpoint.id),
        node=node,
        tags_after=updated_tags,
    )


@router.put(
    "/{vm_type}/{vmid}/tags",
    response_model=ProxmoxTagsResponse,
    summary="Replace all Proxmox config tags on a VM or LXC container",
)
async def replace_proxmox_tags(
    vm_type: VmType,
    vmid: int,
    body: ReplaceProxmoxTagsBody,
    session: SessionDep,
    endpoint_id: int | None = Query(
        default=None,
        description="ProxmoxEndpoint primary key (required)",
    ),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> ProxmoxTagsResponse | JSONResponse:
    return await _apply_tags_mutation(
        vm_type=vm_type,
        vmid=vmid,
        session=session,
        endpoint_id=endpoint_id,
        actor=actor,
        node=body.node.strip(),
        tags=body.tags,
    )


@router.patch(
    "/{vm_type}/{vmid}/tags",
    response_model=ProxmoxTagsResponse,
    summary="Add and/or remove Proxmox config tags on a VM or LXC container",
)
async def patch_proxmox_tags(
    vm_type: VmType,
    vmid: int,
    body: PatchProxmoxTagsBody,
    session: SessionDep,
    endpoint_id: int | None = Query(
        default=None,
        description="ProxmoxEndpoint primary key (required)",
    ),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> ProxmoxTagsResponse | JSONResponse:
    endpoint = await _gate(session, endpoint_id)
    if isinstance(endpoint, JSONResponse):
        return endpoint

    node = body.node.strip()
    add_tags = _normalize_tag_list(body.add)
    remove_tags = {str(tag).strip() for tag in body.remove if str(tag).strip()}

    try:
        proxmox = await _open_proxmox_session(endpoint)
        current_tags = await _get_current_tags(proxmox, vmid, node, vm_type)
        updated_tags = [tag for tag in current_tags if tag not in remove_tags]
        for tag in add_tags:
            if tag not in updated_tags:
                updated_tags.append(tag)
        if updated_tags != current_tags:
            await _set_tags(proxmox, vmid, node, vm_type, updated_tags)
        logger.info(
            "proxmox_tags: patched %s vmid=%s node=%s endpoint_id=%s actor=%s",
            vm_type,
            vmid,
            node,
            endpoint.id,
            actor or "unknown",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "proxmox_tags: failed to patch %s vmid=%s node=%s: %s",
            vm_type,
            vmid,
            node,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Proxmox API call failed: {exc}",
        ) from exc

    return ProxmoxTagsResponse(
        vmid=vmid,
        vm_type=vm_type,
        endpoint_id=int(endpoint.id),
        node=node,
        tags_after=updated_tags,
    )
