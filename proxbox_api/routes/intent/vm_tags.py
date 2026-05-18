"""Intent routes for adding/removing Proxmox tags on VMs without destroying them.

These two PUT endpoints are called by netbox-proxbox's ``proxmox_tags`` module
as a best-effort step when a safe-delete intent is created (tag) or when the
deletion TTL cron expires and the deletion is cancelled (untag).

``endpoint_id`` is optional. When omitted the route auto-resolves to the single
ProxmoxEndpoint that has ``allow_writes=True``. If multiple write-enabled
endpoints exist, the caller must supply ``endpoint_id`` explicitly.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import select

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.proxmox_to_netbox import parse_proxmox_tags
from proxbox_api.routes.intent.dispatchers.common import tags_to_config
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()

VMKind = Literal["qemu", "lxc"]


class TagPendingDeletionBody(BaseModel):
    vmid: int
    node: str
    kind: VMKind
    tag: str


class TagPendingDeletionResponse(BaseModel):
    ok: bool
    vmid: int
    node: str
    kind: VMKind
    tag: str
    tags_after: list[str]


async def _resolve_endpoint(
    session: SessionDep, endpoint_id: int | None
) -> JSONResponse | ProxmoxEndpoint:
    """Resolve a write-enabled ProxmoxEndpoint.

    When ``endpoint_id`` is supplied, delegates to ``_gate`` for the standard
    allow_writes check. When omitted, auto-selects the single endpoint that has
    ``allow_writes=True``. Returns a JSONResponse on any resolution failure.
    """
    if endpoint_id is not None:
        return await _gate(session, endpoint_id)

    result = await _maybe_await(
        session.exec(select(ProxmoxEndpoint).where(ProxmoxEndpoint.allow_writes == True))  # noqa: E712
    )
    endpoints = result.all()

    if not endpoints:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "reason": "no_write_enabled_endpoint",
                "detail": (
                    "No ProxmoxEndpoint with allow_writes=True exists. "
                    "Enable allow_writes on the target endpoint in NetBox."
                ),
            },
        )

    if len(endpoints) > 1:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "reason": "ambiguous_endpoint",
                "detail": (
                    "Multiple write-enabled ProxmoxEndpoints found. "
                    "Pass endpoint_id as a query parameter to target a specific cluster."
                ),
                "endpoint_ids": [e.id for e in endpoints],
            },
        )

    endpoint = endpoints[0]
    logger.debug(
        "intent.vm_tags: auto-resolved ProxmoxEndpoint id=%s (%s)",
        endpoint.id,
        getattr(endpoint, "name", ""),
    )
    return endpoint


async def _get_current_tags(proxmox: object, vmid: int, node: str, kind: VMKind) -> list[str]:
    if kind == "qemu":
        config = await resolve_async(
            proxmox.session.nodes(node).qemu(vmid).config.get()  # type: ignore[union-attr]
        )
    else:
        config = await resolve_async(
            proxmox.session.nodes(node).lxc(vmid).config.get()  # type: ignore[union-attr]
        )
    raw = None
    if hasattr(config, "tags"):
        raw = config.tags
    elif isinstance(config, dict):
        raw = config.get("tags")
    return parse_proxmox_tags(raw)


async def _set_tags(proxmox: object, vmid: int, node: str, kind: VMKind, tags: list[str]) -> None:
    tags_str = tags_to_config(tags)
    if kind == "qemu":
        await resolve_async(
            proxmox.session.nodes(node).qemu(vmid).config.put(tags=tags_str)  # type: ignore[union-attr]
        )
    else:
        await resolve_async(
            proxmox.session.nodes(node).lxc(vmid).config.put(tags=tags_str)  # type: ignore[union-attr]
        )


@router.put(
    "/tag-pending-deletion",
    response_model=TagPendingDeletionResponse,
    summary="Add a pending-deletion tag to a Proxmox VM without destroying it",
)
async def tag_pending_deletion(
    body: TagPendingDeletionBody,
    session: SessionDep,
    endpoint_id: int | None = Query(
        default=None,
        description="ProxmoxEndpoint primary key; auto-resolved when omitted and exactly one write-enabled endpoint exists",
    ),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> TagPendingDeletionResponse | JSONResponse:
    endpoint = await _resolve_endpoint(session, endpoint_id)
    if isinstance(endpoint, JSONResponse):
        return endpoint

    try:
        proxmox = await _open_proxmox_session(endpoint)
        current_tags = await _get_current_tags(proxmox, body.vmid, body.node, body.kind)
        if body.tag not in current_tags:
            updated_tags = current_tags + [body.tag]
            await _set_tags(proxmox, body.vmid, body.node, body.kind, updated_tags)
        else:
            updated_tags = current_tags
        logger.info(
            "intent.vm_tags: added tag %r to %s vmid=%s node=%s actor=%s",
            body.tag,
            body.kind,
            body.vmid,
            body.node,
            actor or "unknown",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "intent.vm_tags: failed to add tag %r to %s vmid=%s node=%s: %s",
            body.tag,
            body.kind,
            body.vmid,
            body.node,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Proxmox API call failed: {exc}",
        ) from exc

    return TagPendingDeletionResponse(
        ok=True,
        vmid=body.vmid,
        node=body.node,
        kind=body.kind,
        tag=body.tag,
        tags_after=updated_tags,
    )


@router.put(
    "/untag-pending-deletion",
    response_model=TagPendingDeletionResponse,
    summary="Remove a pending-deletion tag from a Proxmox VM",
)
async def untag_pending_deletion(
    body: TagPendingDeletionBody,
    session: SessionDep,
    endpoint_id: int | None = Query(
        default=None,
        description="ProxmoxEndpoint primary key; auto-resolved when omitted and exactly one write-enabled endpoint exists",
    ),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> TagPendingDeletionResponse | JSONResponse:
    endpoint = await _resolve_endpoint(session, endpoint_id)
    if isinstance(endpoint, JSONResponse):
        return endpoint

    try:
        proxmox = await _open_proxmox_session(endpoint)
        current_tags = await _get_current_tags(proxmox, body.vmid, body.node, body.kind)
        if body.tag in current_tags:
            updated_tags = [t for t in current_tags if t != body.tag]
            await _set_tags(proxmox, body.vmid, body.node, body.kind, updated_tags)
        else:
            updated_tags = current_tags
        logger.info(
            "intent.vm_tags: removed tag %r from %s vmid=%s node=%s actor=%s",
            body.tag,
            body.kind,
            body.vmid,
            body.node,
            actor or "unknown",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "intent.vm_tags: failed to remove tag %r from %s vmid=%s node=%s: %s",
            body.tag,
            body.kind,
            body.vmid,
            body.node,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Proxmox API call failed: {exc}",
        ) from exc

    return TagPendingDeletionResponse(
        ok=True,
        vmid=body.vmid,
        node=body.node,
        kind=body.kind,
        tag=body.tag,
        tags_after=updated_tags,
    )
