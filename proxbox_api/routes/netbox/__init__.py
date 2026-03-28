"""NetBox route handlers for endpoint and status operations."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select

from proxbox_api.database import DatabaseSessionDep as SessionDep
from proxbox_api.database import NetBoxEndpoint
from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.exception import ProxboxException

# FastAPI Router
router = APIRouter()

#
# Endpoints: /netbox/<endpoint>
#


def _normalize_netbox_endpoint_fields(nb: NetBoxEndpoint) -> None:
    nb.token_version = (nb.token_version or "v1").strip().lower()
    if nb.token_version not in ("v1", "v2"):
        nb.token_version = "v1"
    if nb.token_version == "v1":
        nb.token_key = None
    elif nb.token_key is not None:
        stripped = nb.token_key.strip()
        nb.token_key = stripped if stripped else None
    nb.token = (nb.token or "").strip()


def _validate_netbox_credentials(nb: NetBoxEndpoint) -> None:
    secret = (nb.token or "").strip()
    key = (nb.token_key or "").strip() if nb.token_key else ""
    if nb.token_version == "v1":
        if not secret:
            raise HTTPException(
                status_code=400,
                detail="token is required for NetBox API token v1",
            )
        nb.token = secret
        return
    if not secret or not key:
        raise HTTPException(
            status_code=400,
            detail="token_key and token (secret) must both be set for NetBox API token v2",
        )
    nb.token = secret
    nb.token_key = key


@router.post("/endpoint")
def create_netbox_endpoint(netbox: NetBoxEndpoint, session: SessionDep) -> NetBoxEndpoint:
    existing_any = session.exec(select(NetBoxEndpoint)).first()
    if existing_any:
        raise HTTPException(status_code=400, detail="Only one NetBox endpoint is allowed")

    if session.exec(select(NetBoxEndpoint).where(NetBoxEndpoint.name == netbox.name)).first():
        raise HTTPException(status_code=400, detail="NetBox endpoint name already exists")
    _normalize_netbox_endpoint_fields(netbox)
    _validate_netbox_credentials(netbox)
    session.add(netbox)
    session.commit()
    session.refresh(netbox)
    return netbox


@router.get("/endpoint")
def get_netbox_endpoints(
    session: SessionDep, offset: int = 0, limit: Annotated[int, Query(le=100)] = 100
) -> list[NetBoxEndpoint]:
    netbox_endpoints = session.exec(select(NetBoxEndpoint).offset(offset).limit(limit)).all()
    return list(netbox_endpoints)


GetNetBoxEndpoint = Annotated[list[NetBoxEndpoint], Depends(get_netbox_endpoints)]


@router.get("/endpoint/{netbox_id}")
def get_netbox_endpoint(netbox_id: int, session: SessionDep) -> NetBoxEndpoint:
    netbox_endpoint = session.get(NetBoxEndpoint, netbox_id)
    if not netbox_endpoint:
        raise HTTPException(status_code=404, detail="Netbox Endpoint not found")
    return netbox_endpoint


@router.put("/endpoint/{netbox_id}")
def update_netbox_endpoint(
    netbox_id: int, netbox: NetBoxEndpoint, session: SessionDep
) -> NetBoxEndpoint:
    db_netbox = session.get(NetBoxEndpoint, netbox_id)
    if not db_netbox:
        raise HTTPException(status_code=404, detail="NetBox Endpoint not found")

    for key, value in netbox.model_dump(exclude_unset=True).items():
        setattr(db_netbox, key, value)

    _normalize_netbox_endpoint_fields(db_netbox)
    _validate_netbox_credentials(db_netbox)

    session.add(db_netbox)
    session.commit()
    session.refresh(db_netbox)
    return db_netbox


@router.delete("/endpoint/{netbox_id}")
def delete_netbox_endpoint(netbox_id: int, session: SessionDep) -> dict:
    netbox_endpoint = session.get(NetBoxEndpoint, netbox_id)
    if not netbox_endpoint:
        raise HTTPException(status_code=404, detail="NetBox Endpoint not found.")
    session.delete(netbox_endpoint)
    session.commit()
    return {"message": "NetBox Endpoint deleted."}


@router.get("/status")
async def netbox_status(netbox_session: NetBoxSessionDep):
    """
    ### Asynchronously retrieves the status of the Netbox session.

    **Returns:**
    - The status of the Netbox session.
    """

    try:
        return netbox_session.status()
    except Exception as error:
        raise ProxboxException(
            message="Error fetching status from NetBox API.",
            python_exception=str(error),
        )


@router.get("/openapi")
async def netbox_openapi(netbox_session: NetBoxSessionDep):
    """
    ### Fetches the OpenAPI documentation from the Netbox session.

    **Returns:**
    - **dict:** The OpenAPI documentation retrieved from the Netbox session.
    """

    try:
        output = netbox_session.openapi()
        return output
    except Exception as error:
        raise ProxboxException(
            message="Error fetching OpenAPI documentation from NetBox API.",
            python_exception=str(error),
        )
