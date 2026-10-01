"""Standalone orphan VM sweep routes.

The netbox-proxbox plugin drives each sync stage as its own request and never calls the
full-update routes, so it needs an explicit end-of-run sweep. The sweep is a soft delete:
orphaned VMs become ``decommissioning`` and receive the soft-delete marker tag; no VM is
ever removed from NetBox here.
"""

import asyncio
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.logger import logger
from proxbox_api.routes.proxmox.cluster import cluster_resources
from proxbox_api.services.sync.orphan_sweep import (
    LiveInventoryError,
    LiveVmKey,
    build_live_vm_keys,
    is_delete_orphans_enabled,
    run_orphan_vm_sweep,
)
from proxbox_api.services.sync.sync_state_writer import reset_sidecar_availability_cache
from proxbox_api.services.sync.vmid_helpers import extract_proxmox_session_endpoint_id
from proxbox_api.session.proxmox_providers import (
    ProxmoxPartialSessions,
    close_proxmox_sessions,
    parse_endpoint_ids,
    proxmox_sessions_partial,
)
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_stream_generator

router = APIRouter()

RunIdQuery = Annotated[
    str,
    Query(
        min_length=1,
        title="Run ID",
        description=(
            "Run ID the caller passed to the VM stage so every reconciled VM carries it in "
            "its sync-state sidecar. VMs stamped with a different run ID are orphan candidates."
        ),
    ),
]
DryRunQuery = Annotated[
    bool,
    Query(
        title="Dry Run",
        description="Preview the sweep. Orphans are reported as would_delete and never patched.",
    ),
]
VmStageFailedQuery = Annotated[
    bool,
    Query(
        title="VM Stage Failed",
        description=(
            "Set when the VM stage reported failed VMs. The sweep is then skipped entirely "
            "(skipped_reason=vm_stage_failed) because a live VM that failed to reconcile was "
            "not stamped with this run and would look orphaned."
        ),
    ),
]
EndpointIdsQuery = Annotated[
    str | None,
    Query(
        title="Proxmox Endpoint IDs",
        description=(
            "Comma-separated Proxmox endpoint database IDs the run synchronized. When set, "
            "only VMs owned by those endpoints can be swept."
        ),
        max_length=255,
    ),
]
ProxmoxEndpointIdsQuery = Annotated[
    str | None,
    Query(
        title="Proxmox Endpoint IDs (plugin alias)",
        description=(
            "Alias for endpoint_ids used by the netbox-proxbox plugin. "
            "Takes precedence over endpoint_ids when both are provided."
        ),
        max_length=255,
    ),
]


def sweep_scope_dependency(
    endpoint_ids: EndpointIdsQuery = None,
    proxmox_endpoint_ids: ProxmoxEndpointIdsQuery = None,
) -> frozenset[int] | None:
    """Resolve the endpoint scope with the same parsing as the Proxmox session dependency.

    Returning ``None`` means the caller did not scope the run, so every endpoint is swept.
    """
    parsed = parse_endpoint_ids(proxmox_endpoint_ids or endpoint_ids)
    return frozenset(parsed) if parsed is not None else None


SweepScopeDep = Annotated[frozenset[int] | None, Depends(sweep_scope_dependency)]


def require_scope_for_live_sweep(endpoint_ids: frozenset[int] | None, dry_run: bool) -> None:
    """Reject a live (non-dry-run) sweep that names no endpoint scope.

    ``run_id`` and ``vm_stage_failed`` are caller-supplied claims, so an unscoped live
    sweep would let any authenticated caller soft-delete every managed VM. Previews
    (``dry_run=true``) may stay unscoped because they never write.
    """
    if endpoint_ids is None and not dry_run:
        raise HTTPException(
            status_code=422,
            detail=(
                "A non-dry-run orphan sweep requires endpoint_ids (or proxmox_endpoint_ids) "
                "so it can only affect the endpoints the run synchronized."
            ),
        )


@dataclass(frozen=True)
class LiveInventory:
    """Backend-verified live guest inventory for a live sweep.

    ``keys`` is ``None`` when no inventory was requested (previews); ``unavailable``
    means it was required but could not be fetched for every in-scope session.
    """

    keys: frozenset[LiveVmKey] | None = None
    unavailable: bool = False


def _sessions_cover_scope(partial: ProxmoxPartialSessions, scope: frozenset[int]) -> bool:
    if partial.failures or not partial.sessions:
        return False
    resolved = {extract_proxmox_session_endpoint_id(session) for session in partial.sessions}
    return scope <= resolved


async def _fetch_live_inventory(
    partial: ProxmoxPartialSessions, scope: frozenset[int]
) -> LiveInventory:
    if not _sessions_cover_scope(partial, scope):
        logger.warning("Orphan sweep cannot verify live inventory: a scoped session is missing")
        return LiveInventory(unavailable=True)
    try:
        resources = await cluster_resources(partial.sessions)
    except Exception as error:  # noqa: BLE001
        logger.warning("Orphan sweep cannot fetch live Proxmox inventory: %s", error)
        return LiveInventory(unavailable=True)
    try:
        return LiveInventory(keys=build_live_vm_keys(resources))
    except LiveInventoryError as error:
        logger.warning("Orphan sweep cannot identify a live guest: %s", error)
        return LiveInventory(unavailable=True)


async def live_inventory_dependency(
    partial: Annotated[ProxmoxPartialSessions, Depends(proxmox_sessions_partial)],
    endpoint_ids: SweepScopeDep,
    dry_run: DryRunQuery = False,
) -> LiveInventory:
    """Fetch the live guest inventory of the scoped Proxmox sessions for a live sweep.

    Fails closed: any failed or missing in-scope session, or any fetch error, yields
    ``unavailable=True`` so the sweep skips instead of trusting an incomplete view.
    """
    try:
        if dry_run or endpoint_ids is None:
            return LiveInventory()
        return await _fetch_live_inventory(partial, endpoint_ids)
    finally:
        await close_proxmox_sessions(partial.sessions)


LiveInventoryDep = Annotated[LiveInventory, Depends(live_inventory_dependency)]


@router.get(
    "/orphans/sweep",
    dependencies=[Depends(reset_sidecar_availability_cache)],
)
async def sweep_orphan_virtual_machines(
    netbox_session: NetBoxSessionDep,
    run_id: RunIdQuery,
    endpoint_ids: SweepScopeDep,
    live_inventory: LiveInventoryDep,
    dry_run: DryRunQuery = False,
    vm_stage_failed: VmStageFailedQuery = False,
) -> dict[str, object]:
    """Soft-delete Proxbox-managed VMs that the run identified by ``run_id`` did not touch.

    Honors the ``delete_orphans`` setting: when it is off and ``dry_run`` is false the
    result reports ``enabled=false`` and nothing is patched.
    """
    require_scope_for_live_sweep(endpoint_ids, dry_run)
    return await run_orphan_vm_sweep(
        netbox_session,
        run_id=run_id,
        enabled=is_delete_orphans_enabled(),
        dry_run=dry_run,
        endpoint_ids=endpoint_ids,
        vm_stage_failed=vm_stage_failed,
        live_vm_keys=live_inventory.keys,
        live_inventory_unavailable=live_inventory.unavailable,
    )


@router.get(
    "/orphans/sweep/stream",
    response_model=None,
    dependencies=[Depends(reset_sidecar_availability_cache)],
)
async def sweep_orphan_virtual_machines_stream(
    netbox_session: NetBoxSessionDep,
    run_id: RunIdQuery,
    endpoint_ids: SweepScopeDep,
    live_inventory: LiveInventoryDep,
    dry_run: DryRunQuery = False,
    vm_stage_failed: VmStageFailedQuery = False,
) -> StreamingResponse:
    require_scope_for_live_sweep(endpoint_ids, dry_run)
    enabled = is_delete_orphans_enabled()

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sweep():
            try:
                return await run_orphan_vm_sweep(
                    netbox_session,
                    run_id=run_id,
                    enabled=enabled,
                    dry_run=dry_run,
                    stream=bridge,
                    endpoint_ids=endpoint_ids,
                    vm_stage_failed=vm_stage_failed,
                    live_vm_keys=live_inventory.keys,
                    live_inventory_unavailable=live_inventory.unavailable,
                )
            finally:
                await bridge.close()

        sweep_task = asyncio.create_task(_run_sweep())
        async for frame in sse_stream_generator(
            bridge,
            sweep_task,
            "sweep-orphans",
            started_message=(
                "Previewing orphan virtual machine sweep."
                if dry_run
                else "Starting orphan virtual machine sweep."
            ),
            completed_message="Orphan virtual machine sweep finished.",
        ):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
