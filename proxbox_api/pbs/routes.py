"""Read-only PBS routes mounted under ``/pbs``.

Every handler accepts an optional ``netbox_branch_schema_id`` query parameter so
PBS-to-NetBox writes can be funnelled into a NetBox-branching schema, mirroring
the ``/sync/individual/cluster`` contract. v1 does not actually issue NetBox
writes — the routes fetch from PBS and return summaries — but the parameter is
threaded through so the surface stays stable when sync wiring lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import select

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import PBSEndpoint
from proxbox_api.logger import logger
from proxbox_api.pbs.schemas import (
    PBSStatusItem,
    PBSStatusResponse,
    PBSSyncResponse,
    PBSSyncSummary,
)
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

if TYPE_CHECKING:
    from proxmox_sdk.pbs import PBSClient

router = APIRouter()


async def _list_endpoints(session: SessionDep) -> list[PBSEndpoint]:
    result = await _maybe_await(session.exec(select(PBSEndpoint)))
    return list(result.all())


def _client_for(endpoint: PBSEndpoint) -> "PBSClient":
    """Build a PBS client, converting missing-extras into a 503.

    Importing :mod:`proxbox_api.pbs.session` is safe even without
    ``proxmox-sdk[pbs]`` installed; the deferred import lives in
    :func:`build_pbs_client`.
    """
    try:
        from proxbox_api.pbs.session import build_pbs_client  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=503,
            detail=f"PBS extras unavailable: {exc}",
        ) from exc

    try:
        return build_pbs_client(endpoint)
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(f"PBS extras unavailable: install proxmox-sdk[pbs] to enable /pbs/* ({exc})"),
        ) from exc


@router.get("/status", response_model=PBSStatusResponse)
async def pbs_status(session: SessionDep) -> PBSStatusResponse:
    """Report reachability for every configured PBSEndpoint."""
    items: list[PBSStatusItem] = []
    for endpoint in await _list_endpoints(session):
        item = PBSStatusItem(
            endpoint_id=endpoint.id or 0,
            name=endpoint.name,
            host=endpoint.host,
            port=endpoint.port,
            reachable=False,
        )
        try:
            client = _client_for(endpoint)
        except HTTPException as http_exc:
            item.reason = str(http_exc.detail)
            items.append(item)
            continue

        try:
            version = await client.version()
            item.reachable = True
            item.version = getattr(version, "version", None)
        except Exception as exc:  # noqa: BLE001
            item.reason = f"{type(exc).__name__}: {exc}"
            logger.info("PBS status probe failed for endpoint %s: %s", endpoint.name, exc)
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        items.append(item)
    return PBSStatusResponse(items=items)


async def _sync_one(  # noqa: C901 — explicit branching keeps the loop linear
    endpoint: PBSEndpoint,
    resource: str,
    netbox_branch_schema_id: str | None,
) -> PBSSyncSummary:
    summary = PBSSyncSummary(
        endpoint_id=endpoint.id or 0,
        name=endpoint.name,
        resource=resource,  # type: ignore[arg-type]
        netbox_branch_schema_id=netbox_branch_schema_id,
    )
    try:
        client = _client_for(endpoint)
    except HTTPException as http_exc:
        summary.errors.append(str(http_exc.detail))
        return summary

    try:
        if resource in ("datastores", "full"):
            stores = await client.datastores.list()
            summary.fetched += len(stores)
        if resource in ("snapshots", "full"):
            stores = stores if resource == "full" else await client.datastores.list()
            snapshot_total = 0
            for store in stores:
                snaps = await client.snapshots.list(store.name)
                snapshot_total += len(snaps)
            if resource == "snapshots":
                summary.fetched = snapshot_total
            else:
                summary.fetched += snapshot_total
        if resource in ("jobs", "full"):
            jobs = await client.jobs.list()
            if resource == "jobs":
                summary.fetched = len(jobs)
            else:
                summary.fetched += len(jobs)
        if resource == "node":
            status = await client.nodes.status("localhost")
            summary.fetched = 1 if status else 0
    except Exception as exc:  # noqa: BLE001
        summary.errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass

    return summary


async def _sync_all(
    session: SessionDep,
    resource: str,
    netbox_branch_schema_id: str | None,
) -> PBSSyncResponse:
    endpoints = await _list_endpoints(session)
    items = [await _sync_one(ep, resource, netbox_branch_schema_id) for ep in endpoints]
    return PBSSyncResponse(items=items)


@router.get("/sync/full", response_model=PBSSyncResponse)
async def pbs_sync_full(
    session: SessionDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> PBSSyncResponse:
    return await _sync_all(session, "full", netbox_branch_schema_id)


@router.get("/sync/datastores", response_model=PBSSyncResponse)
async def pbs_sync_datastores(
    session: SessionDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> PBSSyncResponse:
    return await _sync_all(session, "datastores", netbox_branch_schema_id)


@router.get("/sync/snapshots", response_model=PBSSyncResponse)
async def pbs_sync_snapshots(
    session: SessionDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> PBSSyncResponse:
    return await _sync_all(session, "snapshots", netbox_branch_schema_id)


@router.get("/sync/jobs", response_model=PBSSyncResponse)
async def pbs_sync_jobs(
    session: SessionDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> PBSSyncResponse:
    return await _sync_all(session, "jobs", netbox_branch_schema_id)


@router.get("/sync/node", response_model=PBSSyncResponse)
async def pbs_sync_node(
    session: SessionDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> PBSSyncResponse:
    return await _sync_all(session, "node", netbox_branch_schema_id)


__all__ = ["router"]
