"""Read-only PDM routes mounted under ``/pdm``.

Every handler accepts an optional ``netbox_branch_schema_id`` query parameter so
PDM-to-NetBox writes can be funnelled into a NetBox-branching schema, mirroring
the ``/sync/individual/cluster`` contract. v1 does not actually issue NetBox
writes — the routes fetch from PDM and return summaries — but the parameter is
threaded through so the surface stays stable when sync wiring lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import select

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import PDMEndpoint
from proxbox_api.logger import logger
from proxbox_api.pdm.schemas import (
    PDMStatusItem,
    PDMStatusResponse,
    PDMSyncResponse,
    PDMSyncSummary,
)
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

if TYPE_CHECKING:
    from proxmox_sdk.pdm import PDMClient

router = APIRouter()


async def _list_endpoints(session: SessionDep) -> list[PDMEndpoint]:
    result = await _maybe_await(session.exec(select(PDMEndpoint)))
    return list(result.all())


def _client_for(endpoint: PDMEndpoint) -> "PDMClient":
    """Build a PDM client, converting missing-extras into a 503.

    Importing :mod:`proxbox_api.pdm.session` is safe even without
    ``proxmox-sdk[pdm]`` installed; the deferred import lives in
    :func:`build_pdm_client`.
    """
    try:
        from proxbox_api.pdm.session import build_pdm_client  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=503,
            detail=f"PDM extras unavailable: {exc}",
        ) from exc

    try:
        return build_pdm_client(endpoint)
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(f"PDM extras unavailable: install proxmox-sdk[pdm] to enable /pdm/* ({exc})"),
        ) from exc


@router.get("/status", response_model=PDMStatusResponse)
async def pdm_status(session: SessionDep) -> PDMStatusResponse:
    """Report reachability for every configured PDMEndpoint."""
    items: list[PDMStatusItem] = []
    for endpoint in await _list_endpoints(session):
        item = PDMStatusItem(
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
            logger.info("PDM status probe failed for endpoint %s: %s", endpoint.name, exc)
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        items.append(item)
    return PDMStatusResponse(items=items)


async def _sync_one(  # noqa: C901 — explicit branching keeps the loop linear
    endpoint: PDMEndpoint,
    resource: str,
    netbox_branch_schema_id: str | None,
) -> PDMSyncSummary:
    summary = PDMSyncSummary(
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
        if resource in ("remotes", "full"):
            remotes = await client.remotes.list()
            summary.fetched += len(remotes)
        if resource in ("guests", "full"):
            # Use the global resources endpoint filtered to vm/ct types — a
            # single cross-remote call simpler than iterating per-remote.
            guests = await client.resources.list(type="vm")
            containers = await client.resources.list(type="ct")
            guest_total = len(guests) + len(containers)
            if resource == "guests":
                summary.fetched = guest_total
            else:
                summary.fetched += guest_total
        if resource in ("datastores", "full"):
            # Iterate remotes; call PBS datastores only for pbs-type remotes.
            if resource == "datastores":
                remotes = await client.remotes.list()
            ds_total = 0
            for remote in remotes:
                remote_type = getattr(remote, "type", None)
                if remote_type is not None and str(remote_type).lower() != "pbs":
                    continue
                remote_id = getattr(remote, "id", None) or getattr(remote, "name", None)
                if not remote_id:
                    continue
                try:
                    stores = await client.pbs.datastores(str(remote_id))
                    ds_total += len(stores)
                except Exception as exc:  # noqa: BLE001
                    summary.errors.append(
                        f"remote {remote_id}: {type(exc).__name__}: {exc}"
                    )
            if resource == "datastores":
                summary.fetched = ds_total
            else:
                summary.fetched += ds_total
        if resource in ("resources", "full"):
            all_resources = await client.resources.list()
            if resource == "resources":
                summary.fetched = len(all_resources)
            else:
                summary.fetched += len(all_resources)
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
) -> PDMSyncResponse:
    endpoints = await _list_endpoints(session)
    items = [await _sync_one(ep, resource, netbox_branch_schema_id) for ep in endpoints]
    return PDMSyncResponse(items=items)


@router.get("/sync/full", response_model=PDMSyncResponse)
async def pdm_sync_full(
    session: SessionDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> PDMSyncResponse:
    return await _sync_all(session, "full", netbox_branch_schema_id)


@router.get("/sync/remotes", response_model=PDMSyncResponse)
async def pdm_sync_remotes(
    session: SessionDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> PDMSyncResponse:
    return await _sync_all(session, "remotes", netbox_branch_schema_id)


@router.get("/sync/guests", response_model=PDMSyncResponse)
async def pdm_sync_guests(
    session: SessionDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> PDMSyncResponse:
    return await _sync_all(session, "guests", netbox_branch_schema_id)


@router.get("/sync/datastores", response_model=PDMSyncResponse)
async def pdm_sync_datastores(
    session: SessionDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> PDMSyncResponse:
    return await _sync_all(session, "datastores", netbox_branch_schema_id)


@router.get("/sync/resources", response_model=PDMSyncResponse)
async def pdm_sync_resources(
    session: SessionDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> PDMSyncResponse:
    return await _sync_all(session, "resources", netbox_branch_schema_id)


__all__ = ["router"]
