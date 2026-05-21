"""Proxmox access-control endpoints.

Exposes PVE 9.2 API token management operations:
- ``GET /access/tokens/{userid}/{tokenid}`` — read token info.
- ``PUT /access/tokens/{userid}/{tokenid}/regenerate`` — regenerate the
  secret of an existing API token in-place (PVE 9.2+).  All associated
  ACL entries are preserved; no delete-and-recreate required.

All routes proxy the Proxmox ``/access/users/{userid}/token/{tokenid}``
surface and require the caller to specify a ``cluster_name`` when more
than one Proxmox cluster is configured, because token namespaces are
per-cluster.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session_for_request
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


class AccessTokenInfoSchema(BaseModel):
    """Information about a Proxmox API token."""

    cluster_name: str | None = None
    userid: str | None = None
    tokenid: str | None = None
    comment: str | None = None
    privsep: bool | None = None
    expire: int | None = None
    value: str | None = None
    status: str = "ok"
    error: str | None = None


def _to_token_info(
    cluster_name: str, userid: str, tokenid: str, raw: object
) -> AccessTokenInfoSchema:
    data: dict[str, object] = {}
    if hasattr(raw, "model_dump"):
        data = raw.model_dump(mode="python", by_alias=True, exclude_none=True)
    elif isinstance(raw, dict):
        data = dict(raw)
    return AccessTokenInfoSchema(
        cluster_name=cluster_name,
        userid=userid,
        tokenid=tokenid,
        comment=str(data.get("comment")) if data.get("comment") is not None else None,
        privsep=bool(data.get("privsep")) if data.get("privsep") is not None else None,
        expire=int(data["expire"]) if isinstance(data.get("expire"), (int, str)) else None,
        value=str(data.get("value")) if data.get("value") is not None else None,
    )


@router.get("/access/tokens/{userid}/{tokenid}", response_model=AccessTokenInfoSchema | None)
async def read_token(
    pxs: ProxmoxSessionsDep,
    userid: str,
    tokenid: str,
    cluster_name: str | None = Query(None, description="Target cluster name"),
) -> AccessTokenInfoSchema | None:
    """Retrieve info for a Proxmox API token.

    Proxies ``GET /access/users/{userid}/token/{tokenid}``.  Returns
    ``null`` when the token does not exist on any configured cluster.
    """
    for px in pxs:
        if cluster_name and px.name != cluster_name:
            continue
        try:
            raw = await resolve_async(px.session(f"access/users/{userid}/token/{tokenid}").get())
            if raw is not None:
                return _to_token_info(px.name, userid, tokenid, raw)
        except Exception:  # noqa: BLE001
            logger.debug("Token %s/%s not found on cluster %s", userid, tokenid, px.name)
    return None


class TokenRegenerateResponseSchema(BaseModel):
    """Response from a token regenerate request.

    On success, ``value`` contains the new secret.  Store it immediately
    — Proxmox will never return the plaintext secret again.
    """

    cluster_name: str | None = None
    userid: str | None = None
    tokenid: str | None = None
    value: str | None = None
    full_tokenid: str | None = None
    status: str = "ok"
    error: str | None = None


@router.put(
    "/access/tokens/{userid}/{tokenid}/regenerate", response_model=TokenRegenerateResponseSchema
)
async def regenerate_token(
    pxs: ProxmoxSessionsDep,
    userid: str,
    tokenid: str,
    cluster_name: str | None = Query(
        None, description="Target cluster name (required when multiple clusters are configured)"
    ),
) -> TokenRegenerateResponseSchema:
    """Regenerate the secret of an existing API token in-place (PVE 9.2+).

    Proxies ``PUT /access/users/{userid}/token/{tokenid}`` with
    ``regenerate=1``.  All associated ACL entries are preserved.

    **Store the returned ``value`` immediately** — it cannot be retrieved
    again after this call.
    """
    px = resolve_proxmox_session_for_request(
        pxs, cluster_name, resource_name="regenerate API token"
    )
    try:
        raw = await resolve_async(
            px.session(f"access/users/{userid}/token/{tokenid}").put(regenerate=1)
        )
        data: dict[str, object] = {}
        if hasattr(raw, "model_dump"):
            data = raw.model_dump(mode="python", by_alias=True, exclude_none=True)
        elif isinstance(raw, dict):
            data = dict(raw)
        return TokenRegenerateResponseSchema(
            cluster_name=px.name,
            userid=userid,
            tokenid=tokenid,
            value=str(data.get("value")) if data.get("value") is not None else None,
            full_tokenid=str(data.get("full-tokenid") or data.get("full_tokenid"))
            if (data.get("full-tokenid") or data.get("full_tokenid")) is not None
            else None,
        )
    except Exception as exc:
        logger.exception("Error regenerating token %s/%s on cluster %s", userid, tokenid, px.name)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
