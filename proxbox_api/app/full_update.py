"""Full multi-stage synchronization HTTP and SSE endpoints."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from proxbox_api.app.sync_state import (
    acquire_active_sync,
    register_active_sync,
    release_active_sync,
)
from proxbox_api.dependencies import (
    NetBoxSessionDep,
    NetBoxSyncDependenciesDep,
    ProxboxTagDep,
    ResolvedSyncBehaviorFlagsDep,
    ResolvedSyncOverwriteFlagsDep,
)
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.routes.dcim import create_all_device_interfaces
from proxbox_api.routes.proxmox.cluster import (
    ClusterResourcesDep,
    ClusterStatusDep,
)
from proxbox_api.routes.proxmox.cluster import (
    cluster_resources as fetch_cluster_resources,
)
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines
from proxbox_api.routes.virtualization.virtual_machines.backups_vm import (
    _create_all_virtual_machine_backups,
    create_all_virtual_machine_backups,
)
from proxbox_api.routes.virtualization.virtual_machines.disks_vm import (
    create_virtual_disks,
)
from proxbox_api.routes.virtualization.virtual_machines.snapshots_vm import (
    _create_all_virtual_machine_snapshots,
    create_all_virtual_machine_snapshots,
)
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    create_only_vm_interfaces,
    create_only_vm_ip_addresses,
)
from proxbox_api.schemas.stream_messages import ErrorCategory
from proxbox_api.schemas.sync import SyncBehaviorFlags, SyncOverwriteFlags
from proxbox_api.services.sync.backup_routines import sync_all_backup_routines
from proxbox_api.services.sync.devices import create_proxmox_devices
from proxbox_api.services.sync.orphan_sweep import (
    LiveInventoryError,
    build_live_vm_keys,
    extract_touched_vm_ids,
    is_delete_orphans_enabled,
    run_orphan_vm_sweep,
)
from proxbox_api.services.sync.replications import sync_all_replications
from proxbox_api.services.sync.stage_result import result_warnings
from proxbox_api.services.sync.storages import create_storages
from proxbox_api.services.sync.sync_state_writer import reset_sidecar_availability_cache
from proxbox_api.services.sync.task_history import (
    sync_all_virtual_machine_task_histories,
)
from proxbox_api.services.sync.vmid_helpers import extract_proxmox_session_endpoint_id
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event
from proxbox_api.utils.structured_logging import set_operation_id

full_update_router = APIRouter()


def _result_count(value) -> int:
    """Best-effort count for stage payloads returned by sync helpers."""
    if isinstance(value, dict):
        for key in ("count", "created", "devices_count", "virtual_machines_count"):
            raw = value.get(key)
            if isinstance(raw, int):
                return raw
        return 0
    if isinstance(value, list):
        return len(value)
    return 0


def _result_warnings(value) -> list[dict[str, object]]:
    return result_warnings(value)


def _stage_warnings(value, phase: str) -> list[dict[str, object]]:
    """Warnings a stage reported, each tagged with the stage that raised it."""
    return [
        warning if "phase" in warning else {"phase": phase, **warning}
        for warning in result_warnings(value)
    ]


def _stage_result(count: int, value, phase: str) -> dict[str, object]:
    """Step result for a stage that may have finished degraded."""
    result: dict[str, object] = {"count": count}
    warnings = _stage_warnings(value, phase)
    if warnings:
        result.update({"degraded": True, "warnings": warnings})
    return result


def _empty_if_none(value: Any) -> Any:
    """Default a missing stage result to an empty list.

    Deliberately not ``value or []``: an empty result that carries warnings is
    falsy, and ``or`` would replace it and silently lose them.
    """
    return [] if value is None else value


def _backup_rows(value: Any) -> Any:
    """Backup rows from a backup stage result, whether bare or degraded-wrapped."""
    if isinstance(value, dict) and isinstance(value.get("backups"), list):
        return value["backups"]
    return value


# Query parameters that narrow the Proxmox sessions a run acts on. A run using any of
# them must only sweep orphans owned by the endpoints it actually synchronized.
_SESSION_SCOPE_QUERY_PARAMS = (
    "endpoint_ids",
    "proxmox_endpoint_ids",
    "name",
    "domain",
    "ip_address",
)


def orphan_sweep_endpoint_scope(
    request: Request,
    pxs: ProxmoxSessionsDep,
) -> frozenset[int] | None:
    """Return the endpoint ids an orphan sweep may touch, or ``None`` when unscoped.

    The scope is derived from the Proxmox sessions the run used, so an
    endpoint-limited full update never marks another endpoint's VMs.
    """
    scoped = any(
        (request.query_params.get(name) or "").strip() for name in _SESSION_SCOPE_QUERY_PARAMS
    )
    if not scoped:
        return None
    endpoint_ids = (extract_proxmox_session_endpoint_id(session) for session in pxs)
    return frozenset(endpoint_id for endpoint_id in endpoint_ids if endpoint_id is not None)


OrphanSweepScopeDep = Annotated[frozenset[int] | None, Depends(orphan_sweep_endpoint_scope)]


def _vm_stage_rows(vm_result: Any) -> Any:
    """VM rows from the VM stage result, whether bare or degraded-wrapped."""
    if isinstance(vm_result, dict) and isinstance(vm_result.get("virtual_machines"), list):
        return vm_result["virtual_machines"]
    return vm_result


def _vm_stage_failed(vm_result: object) -> bool:
    """Return whether the VM stage reported VMs that failed to reconcile."""
    if isinstance(vm_result, dict):
        failed_count = vm_result.get("failed_count", 0)
    else:
        failed_count = getattr(vm_result, "failed_count", 0)
    return isinstance(failed_count, int) and failed_count > 0


async def _fresh_sweep_inventory(pxs) -> dict[str, object]:
    """Fetch live guest inventory at the sweep boundary as ``run_orphan_vm_sweep`` kwargs.

    The request's original cluster resources predate the sync, so a guest created
    meanwhile would look absent. Any fetch or identification failure fails closed.
    """
    try:
        keys = build_live_vm_keys(await fetch_cluster_resources(pxs))
    except (LiveInventoryError, Exception) as error:  # noqa: BLE001
        logger.warning("Orphan sweep cannot fetch fresh live inventory: %s", error)
        return {"live_vm_keys": None, "live_inventory_unavailable": True}
    return {"live_vm_keys": keys, "live_inventory_unavailable": False}


@full_update_router.get(
    "/full-update",
    dependencies=[Depends(reset_sidecar_availability_cache)],
)
async def full_update_sync(
    netbox_session: NetBoxSessionDep,
    _sync_deps: NetBoxSyncDependenciesDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    overwrite_flags: ResolvedSyncOverwriteFlagsDep = SyncOverwriteFlags(),
    behavior_flags: ResolvedSyncBehaviorFlagsDep = SyncBehaviorFlags(),
    fetch_max_concurrency: Annotated[
        int | None,
        Query(
            ge=1,
            title="Max Fetch Concurrency",
            description="Maximum number of concurrent Proxmox fetch operations.",
        ),
    ] = None,
    netbox_branch_schema_id: Annotated[
        str | None,
        Query(
            description=(
                "Optional netbox-branching schema_id (X-NetBox-Branch). When set, all "
                "NetBox writes performed by this sync run carry the header so changes "
                "land on the branch rather than main."
            ),
        ),
    ] = None,
    sweep_endpoint_ids: OrphanSweepScopeDep = None,
) -> dict:
    return await _full_update_sync_run(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        tag=tag,
        overwrite_flags=overwrite_flags,
        behavior_flags=behavior_flags,
        fetch_max_concurrency=fetch_max_concurrency,
        netbox_branch_schema_id=netbox_branch_schema_id,
        sweep_endpoint_ids=sweep_endpoint_ids,
    )


async def _full_update_sync_run(
    *,
    netbox_session,
    pxs,
    cluster_status,
    cluster_resources,
    tag,
    overwrite_flags: SyncOverwriteFlags,
    behavior_flags: SyncBehaviorFlags,
    fetch_max_concurrency: int | None,
    netbox_branch_schema_id: str | None,
    sweep_endpoint_ids: frozenset[int] | None = None,
) -> dict:
    branch_scope = (
        netbox_session.activate_branch(netbox_branch_schema_id)
        if netbox_branch_schema_id
        else nullcontext()
    )
    async with branch_scope:
        return await _full_update_sync_impl(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            tag=tag,
            overwrite_flags=overwrite_flags,
            behavior_flags=behavior_flags,
            fetch_max_concurrency=fetch_max_concurrency,
            sweep_endpoint_ids=sweep_endpoint_ids,
        )


StageFunction = Callable[..., Awaitable[Any]]
StageNormalizer = Callable[[Any], Any]
StageResultMapper = Callable[[Any], dict[str, object]]


def _identity(value: Any) -> Any:
    return value


def _normalize_rest_backups(value: Any) -> Any:
    return _backup_rows(_empty_if_none(value))


def _normalize_stream_backups(value: Any) -> Any:
    return _empty_if_none(value)


def _list_count_result(value: Any) -> dict[str, object]:
    return {"count": len(value)}


def _vm_count_result(value: Any) -> dict[str, object]:
    return {"count": len(_vm_stage_rows(value))}


def _summary_count_result(value: Any) -> dict[str, object]:
    return {"count": _result_count(value)}


def _degraded_summary_result(phase: str, value: Any) -> dict[str, object]:
    return _stage_result(_result_count(value), value, phase)


def _degraded_list_result(phase: str, value: Any) -> dict[str, object]:
    return _stage_result(len(value), value, phase)


def _update_counts_result(value: Any) -> dict[str, object]:
    return {
        "created": value.get("created", 0),
        "updated": value.get("updated", 0),
    }


def _raw_mapping_result(value: Any) -> dict[str, object]:
    return value


@dataclass(frozen=True, slots=True)
class _FullUpdateInputs:
    netbox_session: Any
    pxs: Any
    cluster_status: Any
    cluster_resources: Any
    tag: Any
    overwrite_flags: SyncOverwriteFlags
    behavior_flags: SyncBehaviorFlags
    fetch_max_concurrency: int | None
    sweep_endpoint_ids: frozenset[int] | None


@dataclass(frozen=True, slots=True)
class _FullUpdateContext:
    inputs: _FullUpdateInputs
    operation_id: str
    tag_refs: list[dict[str, object]]
    dry_run: bool = False


@dataclass(slots=True)
class _StageState:
    results: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _StageDescriptor:
    name: str
    started_message: str
    completed_message: str
    error_message: str
    log_message: str
    rest_callable: StageFunction
    rest_kwargs: dict[str, Any]
    stream_callable: StageFunction
    stream_kwargs: dict[str, Any]
    stream_result_mapper: StageResultMapper
    stream_bridge_keyword: str | None = "websocket"
    start_metadata: dict[str, object] | None = None
    warning_phase: str | None = None
    rest_normalizer: StageNormalizer = _identity
    stream_normalizer: StageNormalizer = _identity


_REST_STAGE_ORDER = (
    "devices",
    "storage",
    "virtual-machines",
    "task-history",
    "virtual-disks",
    "backups",
    "snapshots",
    "node-interfaces",
    "vm-interfaces",
    "vm-ip-addresses",
    "replications",
    "backup-routines",
)

_STREAM_STAGE_ORDER = (
    "devices",
    "storage",
    "virtual-machines",
    "virtual-disks",
    "task-history",
    "backups",
    "snapshots",
    "node-interfaces",
    "vm-interfaces",
    "vm-ip-addresses",
    "replications",
    "backup-routines",
)

_DISCOVERY_STAGE_ORDER = (
    "devices",
    "storage",
    "virtual-machines",
    "virtual-disks",
    "task-history",
    "backups",
    "snapshots",
    "replications",
    "backup-routines",
    "node-interfaces",
    "vm-interfaces",
    "vm-ip-addresses",
)


def _build_tag_refs(tag: Any) -> list[dict[str, object]]:
    tag_ref = {
        "name": getattr(tag, "name", None),
        "slug": getattr(tag, "slug", None),
        "color": getattr(tag, "color", None),
    }
    return [tag_ref] if tag_ref.get("name") and tag_ref.get("slug") else []


def _stage_descriptors(context: _FullUpdateContext) -> dict[str, _StageDescriptor]:
    inputs = context.inputs
    overwrite_flags = inputs.overwrite_flags
    fetch_concurrency = (
        inputs.fetch_max_concurrency if inputs.fetch_max_concurrency is not None else 8
    )
    return {
        "devices": _StageDescriptor(
            name="devices",
            started_message="Starting devices synchronization.",
            completed_message="Devices synchronization finished.",
            error_message="Error while syncing nodes.",
            log_message="Error while syncing nodes during full-update",
            rest_callable=create_proxmox_devices,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "clusters_status": inputs.cluster_status,
                "node": None,
                "tag": inputs.tag,
                "use_websocket": False,
                "overwrite_device_role": overwrite_flags.overwrite_device_role,
                "overwrite_device_type": overwrite_flags.overwrite_device_type,
                "overwrite_device_tags": overwrite_flags.overwrite_device_tags,
                "overwrite_flags": overwrite_flags,
            },
            stream_callable=create_proxmox_devices,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "clusters_status": inputs.cluster_status,
                "node": None,
                "tag": inputs.tag,
                "use_websocket": True,
                "overwrite_device_role": overwrite_flags.overwrite_device_role,
                "overwrite_device_type": overwrite_flags.overwrite_device_type,
                "overwrite_device_tags": overwrite_flags.overwrite_device_tags,
                "overwrite_flags": overwrite_flags,
            },
            stream_result_mapper=_list_count_result,
        ),
        "storage": _StageDescriptor(
            name="storage",
            started_message="Starting storage synchronization.",
            completed_message="Storage synchronization finished.",
            error_message="Error while syncing storages.",
            log_message="Error while syncing storages during full-update",
            rest_callable=create_storages,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "tag": inputs.tag,
                "use_websocket": False,
                "fetch_concurrency": fetch_concurrency,
                "overwrite_flags": overwrite_flags,
            },
            stream_callable=create_storages,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "tag": inputs.tag,
                "use_websocket": True,
                "fetch_concurrency": fetch_concurrency,
                "overwrite_flags": overwrite_flags,
            },
            stream_result_mapper=_list_count_result,
        ),
        "virtual-machines": _StageDescriptor(
            name="virtual-machines",
            started_message="Starting virtual machines synchronization.",
            completed_message="Virtual machines synchronization finished.",
            error_message="Error while syncing virtual machines.",
            log_message="Error while syncing virtual machines during full-update",
            rest_callable=create_virtual_machines,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "cluster_resources": inputs.cluster_resources,
                "tag": inputs.tag,
                "use_websocket": False,
                "sync_vm_network": False,
                "sync_task_history": False,
                "overwrite_vm_role": overwrite_flags.overwrite_vm_role,
                "overwrite_vm_type": overwrite_flags.overwrite_vm_type,
                "overwrite_vm_tags": overwrite_flags.overwrite_vm_tags,
                "overwrite_vm_description": overwrite_flags.overwrite_vm_description,
                "overwrite_vm_custom_fields": overwrite_flags.overwrite_vm_custom_fields,
                "overwrite_flags": overwrite_flags,
                "behavior_flags": inputs.behavior_flags,
                "run_id": context.operation_id,
            },
            stream_callable=create_virtual_machines,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "cluster_resources": inputs.cluster_resources,
                "tag": inputs.tag,
                "use_websocket": True,
                "sync_vm_network": False,
                "sync_task_history": False,
                "overwrite_vm_role": overwrite_flags.overwrite_vm_role,
                "overwrite_vm_type": overwrite_flags.overwrite_vm_type,
                "overwrite_vm_tags": overwrite_flags.overwrite_vm_tags,
                "overwrite_vm_description": overwrite_flags.overwrite_vm_description,
                "overwrite_vm_custom_fields": overwrite_flags.overwrite_vm_custom_fields,
                "overwrite_flags": overwrite_flags,
                "behavior_flags": inputs.behavior_flags,
                "run_id": context.operation_id,
            },
            stream_result_mapper=_vm_count_result,
            warning_phase="virtual-machines",
        ),
        "virtual-disks": _StageDescriptor(
            name="virtual-disks",
            started_message="Starting virtual disks synchronization.",
            completed_message="Virtual disks synchronization finished.",
            error_message="Error while syncing virtual disks.",
            log_message="Error while syncing virtual disks during full-update",
            rest_callable=create_virtual_disks,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "cluster_resources": inputs.cluster_resources,
                "tag": inputs.tag,
                "use_websocket": False,
                "use_css": False,
                "fetch_max_concurrency": inputs.fetch_max_concurrency,
            },
            stream_callable=create_virtual_disks,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "cluster_resources": inputs.cluster_resources,
                "tag": inputs.tag,
                "use_websocket": True,
                "use_css": False,
                "fetch_max_concurrency": inputs.fetch_max_concurrency,
            },
            stream_result_mapper=partial(_degraded_summary_result, "virtual-disks"),
            warning_phase="virtual-disks",
        ),
        "task-history": _StageDescriptor(
            name="task-history",
            started_message="Starting task history synchronization.",
            completed_message="Task history synchronization finished.",
            error_message="Error while syncing task history.",
            log_message="Error while syncing task history during full-update",
            rest_callable=sync_all_virtual_machine_task_histories,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "tag_refs": context.tag_refs,
                "fetch_max_concurrency": inputs.fetch_max_concurrency,
            },
            stream_callable=sync_all_virtual_machine_task_histories,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "tag_refs": context.tag_refs,
                "use_websocket": True,
                "fetch_max_concurrency": inputs.fetch_max_concurrency,
            },
            stream_result_mapper=_summary_count_result,
        ),
        "backups": _StageDescriptor(
            name="backups",
            started_message="Starting backup synchronization.",
            completed_message="Backup synchronization finished.",
            error_message="Error while syncing backups.",
            log_message="Error while syncing backups during full-update",
            rest_callable=create_all_virtual_machine_backups,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "tag": inputs.tag,
                "delete_nonexistent_backup": True,
                "fetch_max_concurrency": inputs.fetch_max_concurrency,
            },
            stream_callable=_create_all_virtual_machine_backups,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "tag": inputs.tag,
                "delete_nonexistent_backup": True,
                "fetch_max_concurrency": inputs.fetch_max_concurrency,
                "use_websocket": True,
            },
            stream_result_mapper=partial(_degraded_list_result, "backups"),
            warning_phase="backups",
            rest_normalizer=_normalize_rest_backups,
            stream_normalizer=_normalize_stream_backups,
        ),
        "snapshots": _StageDescriptor(
            name="snapshots",
            started_message="Starting snapshot synchronization.",
            completed_message="Snapshot synchronization finished.",
            error_message="Error while syncing snapshots.",
            log_message="Error while syncing snapshots during full-update",
            rest_callable=create_all_virtual_machine_snapshots,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "cluster_resources": inputs.cluster_resources,
                "tag": inputs.tag,
                "fetch_max_concurrency": inputs.fetch_max_concurrency,
            },
            stream_callable=_create_all_virtual_machine_snapshots,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "cluster_resources": inputs.cluster_resources,
                "tag": inputs.tag,
                "fetch_max_concurrency": inputs.fetch_max_concurrency,
                "use_websocket": True,
            },
            stream_result_mapper=partial(_degraded_summary_result, "snapshots"),
            warning_phase="snapshots",
        ),
        "node-interfaces": _StageDescriptor(
            name="node-interfaces",
            started_message="Starting node interfaces synchronization.",
            completed_message="Node interfaces synchronization finished.",
            error_message="Error while syncing node interfaces.",
            log_message="Error while syncing node interfaces during full-update",
            rest_callable=create_all_device_interfaces,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "tag": inputs.tag,
                "clusters_status": inputs.cluster_status,
                "pxs": inputs.pxs,
                "use_websocket": False,
                "behavior_flags": inputs.behavior_flags,
            },
            stream_callable=create_all_device_interfaces,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "tag": inputs.tag,
                "clusters_status": inputs.cluster_status,
                "pxs": inputs.pxs,
                "use_websocket": True,
                "behavior_flags": inputs.behavior_flags,
            },
            stream_result_mapper=partial(_degraded_list_result, "node-interfaces"),
            warning_phase="node-interfaces",
        ),
        "vm-interfaces": _StageDescriptor(
            name="vm-interfaces",
            started_message="Starting VM interfaces synchronization.",
            completed_message="VM interfaces synchronization finished.",
            error_message="Error while syncing VM interfaces.",
            log_message="Error while syncing VM interfaces during full-update",
            rest_callable=create_only_vm_interfaces,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "cluster_resources": inputs.cluster_resources,
                "tag": inputs.tag,
                "use_websocket": False,
                "overwrite_flags": overwrite_flags,
            },
            stream_callable=create_only_vm_interfaces,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "cluster_resources": inputs.cluster_resources,
                "tag": inputs.tag,
                "use_websocket": True,
                "overwrite_flags": overwrite_flags,
            },
            stream_result_mapper=partial(_degraded_list_result, "vm-interfaces"),
            warning_phase="vm-interfaces",
        ),
        "vm-ip-addresses": _StageDescriptor(
            name="vm-ip-addresses",
            started_message="Starting VM IP address synchronization.",
            completed_message="VM IP address synchronization finished.",
            error_message="Error while syncing VM IP addresses.",
            log_message="Error while syncing VM IP addresses during full-update",
            rest_callable=create_only_vm_ip_addresses,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "cluster_resources": inputs.cluster_resources,
                "tag": inputs.tag,
                "use_websocket": False,
                "overwrite_flags": overwrite_flags,
            },
            stream_callable=create_only_vm_ip_addresses,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
                "cluster_status": inputs.cluster_status,
                "cluster_resources": inputs.cluster_resources,
                "tag": inputs.tag,
                "use_websocket": True,
                "overwrite_flags": overwrite_flags,
            },
            stream_result_mapper=partial(_degraded_list_result, "vm-ip-addresses"),
            warning_phase="vm-ip-addresses",
        ),
        "replications": _StageDescriptor(
            name="replications",
            started_message="Starting replications synchronization.",
            completed_message="Replications synchronization finished.",
            error_message="Error while syncing replications.",
            log_message="Error while syncing replications during full-update",
            rest_callable=sync_all_replications,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
            },
            stream_callable=sync_all_replications,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
            },
            stream_result_mapper=_update_counts_result,
            stream_bridge_keyword=None,
        ),
        "backup-routines": _StageDescriptor(
            name="backup-routines",
            started_message="Starting backup routines synchronization.",
            completed_message="Backup routines synchronization finished.",
            error_message="Error while syncing backup routines.",
            log_message="Error while syncing backup routines during full-update",
            rest_callable=sync_all_backup_routines,
            rest_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
            },
            stream_callable=sync_all_backup_routines,
            stream_kwargs={
                "netbox_session": inputs.netbox_session,
                "pxs": inputs.pxs,
            },
            stream_result_mapper=_update_counts_result,
            stream_bridge_keyword="bridge",
        ),
    }


async def _run_stage(
    descriptor: _StageDescriptor,
    state: _StageState,
    *,
    streaming: bool,
    bridge: WebSocketSSEBridge | None = None,
) -> Any:
    try:
        function = descriptor.stream_callable if streaming else descriptor.rest_callable
        kwargs = dict(descriptor.stream_kwargs if streaming else descriptor.rest_kwargs)
        if streaming and descriptor.stream_bridge_keyword is not None:
            kwargs[descriptor.stream_bridge_keyword] = bridge
        raw_result = await function(**kwargs)
        if descriptor.warning_phase is not None:
            state.warnings.extend(_stage_warnings(raw_result, descriptor.warning_phase))
        normalizer = descriptor.stream_normalizer if streaming else descriptor.rest_normalizer
        result = normalizer(raw_result)
        state.results[descriptor.name] = result
        return result
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        if streaming:
            raise
        logger.exception(descriptor.log_message)
        raise ProxboxException(
            message=descriptor.error_message,
            python_exception=str(error),
        ) from error
    finally:
        if bridge is not None:
            await bridge.close()


async def _stream_stage_events(
    descriptor: _StageDescriptor,
    state: _StageState,
) -> AsyncIterator[str]:
    start_payload: dict[str, object] = {
        "step": descriptor.name,
        "status": "started",
        "message": descriptor.started_message,
    }
    if descriptor.start_metadata is not None:
        start_payload["metadata"] = descriptor.start_metadata
    yield sse_event(
        "step",
        start_payload,
    )
    started_at = time.monotonic()
    bridge = WebSocketSSEBridge()
    task = asyncio.create_task(_run_stage(descriptor, state, streaming=True, bridge=bridge))
    async for frame in bridge.iter_sse():
        yield frame
    result = await task
    yield sse_event(
        "step",
        {
            "step": descriptor.name,
            "status": "completed",
            "message": descriptor.completed_message,
            "result": descriptor.stream_result_mapper(result),
            "duration_seconds": round(time.monotonic() - started_at, 3),
        },
    )


async def _run_rest_orphan_sweep(
    context: _FullUpdateContext,
    state: _StageState,
    enabled: bool,
) -> dict[str, object]:
    inputs = context.inputs
    return await run_orphan_vm_sweep(
        inputs.netbox_session,
        run_id=context.operation_id,
        enabled=enabled,
        touched_vm_ids=extract_touched_vm_ids(_vm_stage_rows(state.results["virtual-machines"])),
        endpoint_ids=inputs.sweep_endpoint_ids,
        vm_stage_failed=_vm_stage_failed(state.results["virtual-machines"]),
        **await _fresh_sweep_inventory(inputs.pxs),
    )


async def _run_stream_orphan_sweep(
    context: _FullUpdateContext,
    state: _StageState,
    enabled: bool,
    *,
    stream: WebSocketSSEBridge,
) -> dict[str, object]:
    inputs = context.inputs
    return await run_orphan_vm_sweep(
        inputs.netbox_session,
        run_id=context.operation_id,
        enabled=enabled,
        dry_run=context.dry_run,
        stream=stream,
        touched_vm_ids=extract_touched_vm_ids(_vm_stage_rows(state.results["virtual-machines"])),
        endpoint_ids=inputs.sweep_endpoint_ids,
        vm_stage_failed=_vm_stage_failed(state.results["virtual-machines"]),
        **await _fresh_sweep_inventory(inputs.pxs),
    )


def _orphan_sweep_descriptor(
    context: _FullUpdateContext,
    state: _StageState,
    enabled: bool,
) -> _StageDescriptor:
    preview = context.dry_run
    return _StageDescriptor(
        name="sweep-orphans",
        started_message=(
            "Previewing orphan virtual machine sweep."
            if preview
            else "Starting orphan virtual machine sweep."
        ),
        completed_message=(
            "Orphan virtual machine sweep preview finished."
            if preview
            else "Orphan virtual machine sweep finished."
        ),
        error_message="Error while sweeping orphan virtual machines.",
        log_message="Error while sweeping orphan virtual machines during full-update",
        rest_callable=partial(_run_rest_orphan_sweep, context, state, enabled),
        rest_kwargs={},
        stream_callable=partial(_run_stream_orphan_sweep, context, state, enabled),
        stream_kwargs={},
        stream_result_mapper=_raw_mapping_result,
        stream_bridge_keyword="stream",
        start_metadata={
            "operation_id": context.operation_id,
            "delete_orphans": enabled,
            "dry_run": context.dry_run,
        },
    )


def _build_full_update_result(
    state: _StageState,
    *,
    streaming: bool,
) -> dict[str, object]:
    results = state.results
    virtual_machines = _vm_stage_rows(results["virtual-machines"])
    if streaming:
        result: dict[str, object] = {
            "devices": results["devices"],
            "storage": results["storage"],
            "virtual_machines": virtual_machines,
            "virtual_disks": results["virtual-disks"],
            "task_history": results["task-history"],
            "backups": results["backups"],
            "snapshots": results["snapshots"],
            "node_interfaces": results["node-interfaces"],
            "vm_interfaces": results["vm-interfaces"],
            "vm_ip_addresses": results["vm-ip-addresses"],
            "replications": results["replications"],
            "backup_routines": results["backup-routines"],
            "devices_count": len(results["devices"]),
            "storage_count": len(results["storage"]),
            "virtual_machines_count": len(virtual_machines),
            "virtual_disks_count": _result_count(results["virtual-disks"]),
            "task_history_count": _result_count(results["task-history"]),
            "backups_count": len(results["backups"]),
            "snapshots_count": _result_count(results["snapshots"]),
            "node_interfaces_count": len(results["node-interfaces"]),
            "vm_interfaces_count": len(results["vm-interfaces"]),
            "vm_ip_addresses_count": len(results["vm-ip-addresses"]),
            "replications_count": results["replications"].get("created", 0)
            + results["replications"].get("updated", 0),
            "backup_routines_count": results["backup-routines"].get("created", 0)
            + results["backup-routines"].get("updated", 0),
        }
    else:
        result = {
            "status": "completed",
            "devices": results["devices"],
            "storage": results["storage"],
            "virtual_machines": virtual_machines,
            "virtual_disks": results["virtual-disks"],
            "task_history": results["task-history"],
            "backups": results["backups"],
            "snapshots": results["snapshots"],
            "replications": results["replications"],
            "backup_routines": results["backup-routines"],
            "node_interfaces": results["node-interfaces"],
            "vm_interfaces": results["vm-interfaces"],
            "vm_ip_addresses": results["vm-ip-addresses"],
            "devices_count": len(results["devices"]),
            "storage_count": len(results["storage"]),
            "virtual_machines_count": len(virtual_machines),
            "virtual_disks_count": _result_count(results["virtual-disks"]),
            "task_history_count": _result_count(results["task-history"]),
            "backups_count": len(results["backups"]),
            "snapshots_count": _result_count(results["snapshots"]),
            "replications_count": results["replications"].get("created", 0)
            + results["replications"].get("updated", 0),
            "backup_routines_count": results["backup-routines"].get("created", 0)
            + results["backup-routines"].get("updated", 0),
            "node_interfaces_count": len(results["node-interfaces"]),
            "vm_interfaces_count": len(results["vm-interfaces"]),
            "vm_ip_addresses_count": len(results["vm-ip-addresses"]),
        }
    if "sweep-orphans" in results:
        result["orphan_sweep"] = results["sweep-orphans"]
    if state.warnings:
        result["warnings"] = state.warnings
        result["degraded"] = True
    return result


async def _full_update_sync_impl(
    *,
    netbox_session: Any,
    pxs: Any,
    cluster_status: Any,
    cluster_resources: Any,
    tag: Any,
    overwrite_flags: SyncOverwriteFlags,
    behavior_flags: SyncBehaviorFlags,
    fetch_max_concurrency: int | None,
    sweep_endpoint_ids: frozenset[int] | None = None,
) -> dict[str, object]:
    operation_id = str(uuid.uuid4())
    context = _FullUpdateContext(
        inputs=_FullUpdateInputs(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            tag=tag,
            overwrite_flags=overwrite_flags,
            behavior_flags=behavior_flags,
            fetch_max_concurrency=fetch_max_concurrency,
            sweep_endpoint_ids=sweep_endpoint_ids,
        ),
        operation_id=operation_id,
        tag_refs=_build_tag_refs(tag),
    )
    state = _StageState()
    set_operation_id(operation_id)
    logger.info("Starting full_update sync", extra={"operation_id": operation_id})

    active_entry = await acquire_active_sync(operation_id, kind="full-update")
    try:
        descriptors = _stage_descriptors(context)
        for stage_name in _REST_STAGE_ORDER:
            await _run_stage(descriptors[stage_name], state, streaming=False)
        delete_orphans_enabled = is_delete_orphans_enabled()
        if delete_orphans_enabled:
            descriptor = _orphan_sweep_descriptor(context, state, delete_orphans_enabled)
            await _run_stage(descriptor, state, streaming=False)
        return _build_full_update_result(state, streaming=False)
    finally:
        await release_active_sync(active_entry)


def _stream_discovery_event(operation_id: str) -> str:
    stage_items = [{"name": name, "type": "stage"} for name in _DISCOVERY_STAGE_ORDER]
    return sse_event(
        "discovery",
        {
            "event": "discovery",
            "phase": "full-update",
            "status": "discovered",
            "message": f"Discovered {len(stage_items)} sync stage(s) for full update",
            "count": len(stage_items),
            "items": stage_items,
            "progress": {"current": 0, "total": len(stage_items), "percent": 0},
            "metadata": {"operation_id": operation_id},
        },
    )


def _terminal_error_events(error: asyncio.CancelledError | Exception) -> tuple[str, ...]:
    if isinstance(error, asyncio.CancelledError):
        return (
            sse_event(
                "error",
                {
                    "step": "full-update",
                    "status": "failed",
                    "error": "Server shutdown or request cancelled.",
                    "detail": "Server shutdown or request cancelled.",
                },
            ),
            sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Full update sync cancelled.",
                    "errors": [{"detail": "Server shutdown or request cancelled."}],
                },
            ),
        )
    if isinstance(error, ProxboxException):
        return (
            sse_event(
                "error_detail",
                {
                    "event": "error_detail",
                    "phase": "full-update",
                    "category": ErrorCategory.INTERNAL.value,
                    "message": "Full update synchronization failed",
                    "detail": error.detail or error.message,
                    "suggestion": "Review backend logs and retry the full update",
                },
            ),
            sse_event(
                "error",
                {
                    "step": "full-update",
                    "status": "failed",
                    "error": error.message,
                    "detail": error.detail,
                },
            ),
            sse_event(
                "complete",
                {
                    "ok": False,
                    "message": error.message,
                    "errors": [{"detail": error.detail or error.message}],
                },
            ),
        )
    return (
        sse_event(
            "error_detail",
            {
                "event": "error_detail",
                "phase": "full-update",
                "category": ErrorCategory.INTERNAL.value,
                "message": "Unexpected error during full update",
                "detail": str(error),
                "suggestion": "Check backend logs for stack trace and retry",
            },
        ),
        sse_event(
            "error",
            {
                "step": "full-update",
                "status": "failed",
                "error": str(error),
                "detail": str(error),
            },
        ),
        sse_event(
            "complete",
            {
                "ok": False,
                "message": "Full update sync failed.",
                "errors": [{"detail": str(error)}],
            },
        ),
    )


async def _full_update_event_stream(
    inputs: _FullUpdateInputs,
    bootstrap_payload: dict[str, object],
    *,
    dry_run: bool,
) -> AsyncIterator[str]:
    operation_id = str(uuid.uuid4())
    context = _FullUpdateContext(
        inputs=inputs,
        operation_id=operation_id,
        tag_refs=_build_tag_refs(inputs.tag),
        dry_run=dry_run,
    )
    state = _StageState()
    set_operation_id(operation_id)
    logger.info("Starting full_update sync (stream)", extra={"operation_id": operation_id})

    async with register_active_sync(operation_id, kind="full-update"):
        try:
            yield sse_event("bootstrap_done", bootstrap_payload)
            yield sse_event(
                "step",
                {
                    "step": "stream",
                    "status": "started",
                    "message": f"Full update stream connected (operation_id={operation_id})",
                },
            )
            yield _stream_discovery_event(operation_id)
            descriptors = _stage_descriptors(context)
            for stage_name in _STREAM_STAGE_ORDER:
                async for frame in _stream_stage_events(descriptors[stage_name], state):
                    yield frame
            delete_orphans_enabled = is_delete_orphans_enabled()
            if delete_orphans_enabled or dry_run:
                descriptor = _orphan_sweep_descriptor(context, state, delete_orphans_enabled)
                async for frame in _stream_stage_events(descriptor, state):
                    yield frame
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Full update sync completed.",
                    "result": _build_full_update_result(state, streaming=True),
                },
            )
        except (asyncio.CancelledError, Exception) as error:
            for frame in _terminal_error_events(error):
                yield frame


async def _branched_full_update_stream(
    netbox_session: Any,
    netbox_branch_schema_id: str | None,
    stream: AsyncIterator[str],
) -> AsyncIterator[str]:
    branch_scope = (
        netbox_session.activate_branch(netbox_branch_schema_id)
        if netbox_branch_schema_id
        else nullcontext()
    )
    async with branch_scope:
        async for frame in stream:
            yield frame


@full_update_router.get(
    "/full-update/stream",
    response_model=None,
    dependencies=[Depends(reset_sidecar_availability_cache)],
)
async def full_update_sync_stream(
    netbox_session: NetBoxSessionDep,
    _sync_deps: NetBoxSyncDependenciesDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    overwrite_flags: ResolvedSyncOverwriteFlagsDep = SyncOverwriteFlags(),
    behavior_flags: ResolvedSyncBehaviorFlagsDep = SyncBehaviorFlags(),
    fetch_max_concurrency: Annotated[
        int | None,
        Query(
            ge=1,
            title="Max Fetch Concurrency",
            description="Maximum number of concurrent Proxmox fetch operations.",
        ),
    ] = None,
    dry_run: bool = Query(
        default=False,
        description=(
            "Preview end-of-run orphan marking. When true, orphan VMs are "
            "reported as would_delete events and no PATCH requests are sent."
        ),
    ),
    netbox_branch_schema_id: Annotated[
        str | None,
        Query(
            description=(
                "Optional netbox-branching schema_id (X-NetBox-Branch). When set, all "
                "NetBox writes performed by this sync stream carry the header so changes "
                "land on the branch rather than main."
            ),
        ),
    ] = None,
    sweep_endpoint_ids: OrphanSweepScopeDep = None,
) -> StreamingResponse:
    inputs = _FullUpdateInputs(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        tag=tag,
        overwrite_flags=overwrite_flags,
        behavior_flags=behavior_flags,
        fetch_max_concurrency=fetch_max_concurrency,
        sweep_endpoint_ids=sweep_endpoint_ids,
    )
    stream = _full_update_event_stream(
        inputs,
        _sync_deps.as_dict(),
        dry_run=dry_run,
    )
    return StreamingResponse(
        _branched_full_update_stream(netbox_session, netbox_branch_schema_id, stream),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def register_full_update_routes(app: Any) -> None:
    """Mount full-update routes on the root application."""
    app.include_router(full_update_router)
