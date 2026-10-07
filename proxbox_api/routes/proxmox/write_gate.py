"""Shared write gate for Proxmox routes that act on already-resolved sessions.

Routes that receive ``ProxmoxSessionsDep`` have no ``endpoint_id`` query
parameter, so the target ``ProxmoxEndpoint`` is resolved from the session's
``db_endpoint_id``, but only for sessions whose ``endpoint_source`` is ``database``.
Sessions loaded from NetBox carry a NetBox object id from an overlapping id space, so
they cannot be authorised and are treated as write-disabled.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep, ProxmoxEndpoint
from proxbox_api.utils.async_compat import maybe_await

WRITES_DISABLED_REASON = "endpoint_writes_disabled"


def require_actor(actor: str | None) -> str:
    """Return the stripped ``X-Proxbox-Actor`` value or raise 422."""
    actor_value = (actor or "").strip()
    if not actor_value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "actor_required",
                "detail": "Proxmox writes require the X-Proxbox-Actor header.",
            },
        )
    return actor_value


async def session_writes_allowed(db: AsyncDatabaseSessionDep, px: object) -> bool:
    """Return True only when the session's endpoint has ``allow_writes`` enabled."""
    # ``db_endpoint_id`` is a local ProxmoxEndpoint primary key only for database-sourced
    # sessions. NetBox-sourced sessions carry the NetBox object id, an overlapping id
    # space, so they must never be resolved against the local table.
    if getattr(px, "endpoint_source", None) != "database":
        return False
    endpoint_id = getattr(px, "db_endpoint_id", None)
    if endpoint_id is None:
        return False
    endpoint = await maybe_await(db.get(ProxmoxEndpoint, endpoint_id))
    return bool(endpoint is not None and endpoint.allow_writes)


def writes_disabled_response(cluster_name: str | None) -> JSONResponse:
    """Build the standard 403 response for a write-disabled endpoint."""
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={
            "reason": WRITES_DISABLED_REASON,
            "detail": (
                "Writes are disabled on this endpoint. Enable "
                "ProxmoxEndpoint.allow_writes before changing Proxmox."
            ),
            "cluster_name": cluster_name,
        },
    )
