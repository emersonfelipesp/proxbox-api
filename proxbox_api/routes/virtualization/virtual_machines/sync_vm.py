"""Virtual machine creation sync and SSE stream endpoints."""

# FastAPI Imports
import asyncio
import inspect
import resource as process_resource
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, TypeVar, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from proxbox_api.cache import global_cache
from proxbox_api.constants import VM_ROLE_MAPPINGS, VM_TYPE_MAPPINGS
from proxbox_api.dependencies import (
    NetBoxSessionDep,
    ProxboxTagDep,
    ResolvedSyncBehaviorFlagsDep,
    ResolvedSyncOverwriteFlagsDep,
    ensure_netbox_sync_dependencies,
)
from proxbox_api.enum.status_mapping import ProxmoxToNetBoxVMStatus
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_compat import VirtualMachine
from proxbox_api.netbox_rest import (
    clear_rest_get_cache_for_path,
    rest_create_async,
    rest_first_async,
    rest_list_async,
    rest_list_paginated_async,
    rest_patch_async,
    rest_reconcile_async,
)
from proxbox_api.netbox_version import detect_netbox_version, supports_virtual_machine_type
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxDeviceRoleSyncState,
    NetBoxVirtualDiskSyncState,
    NetBoxVirtualMachineCreateBody,
    NetBoxVirtualMachineInterfaceSyncState,
    NetBoxVlanSyncState,
    ProxmoxVmConfigInput,
    ProxmoxVmResourceInput,
    _parse_proxmox_kv_flag,
)
from proxbox_api.routes.proxmox import get_vm_config
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.routes.virtualization.virtual_machines.helpers import (
    resolve_netbox_write_concurrency,
    resolve_vm_config_fetch_timeout_seconds,
    resolve_vm_sync_concurrency,
)
from proxbox_api.schemas.stream_messages import ErrorCategory, ItemOperation, SubstepStatus
from proxbox_api.schemas.sync import SyncBehaviorFlags, SyncOverwriteFlags
from proxbox_api.services.name_collision import (
    NameResolution,
    resolve_unique_vm_name,
)
from proxbox_api.services.proxmox.tag_styles import fetch_tag_color_map
from proxbox_api.services.proxmox_helpers import (
    fetch_qemu_guest_agent_network_interfaces,
    get_qemu_guest_agent_hostname,
    get_qemu_guest_agent_network_interfaces,
    sanitize_dns_hostname,
)
from proxbox_api.services.sync.devices import (
    _effective_cluster_site_id,
    _ensure_cluster,
    _ensure_cluster_type,
    _ensure_device,
    _ensure_device_type,
    _ensure_manufacturer,
    _ensure_site,
    _resolve_tenant,
)
from proxbox_api.services.sync.devices import (
    _ensure_device_role as _ensure_proxmox_node_role,
)
from proxbox_api.services.sync.guest_vm_interface import (
    VMInterfaceSyncStrategy,
    normalize_vm_interface_sync_strategy,
    reconcile_guest_vm_interfaces,
    should_use_guest_agent_core_interface_name,
    warn_legacy_vm_interface_strategy,
)
from proxbox_api.services.sync.network import (
    _resolve_vm_interface_identity,
    normalize_vm_interface_name,
)
from proxbox_api.services.sync.node_device_name import NodeDeviceNameError, render_node_device_name
from proxbox_api.services.sync.orphan_sweep import (
    clear_soft_delete_marker,
    exclude_soft_deleted_vms,
)
from proxbox_api.services.sync.reconciliation.types import (
    NetBoxVMOperation as _NetBoxVMOperation,
)
from proxbox_api.services.sync.reconciliation.types import (
    PreparedVMState as _PreparedVMState,
)
from proxbox_api.services.sync.reconciliation.vm_queue import (
    build_vm_operation_queue as _build_vm_operation_queue,
)
from proxbox_api.services.sync.reconciliation.vm_queue import (
    build_vm_operation_queue_python as _build_vm_operation_queue_python,
)
from proxbox_api.services.sync.reconciliation.vm_queue import (
    build_vm_snapshot_identity_indexes as _build_vm_snapshot_identity_indexes,
)
from proxbox_api.services.sync.reconciliation.vm_queue import (
    normalize_current_vm_payload as _normalize_current_virtual_machine_payload,
)
from proxbox_api.services.sync.reconciliation.vm_queue import (
    prepared_vm_result_key as _prepared_vm_result_key,
)
from proxbox_api.services.sync.reconciliation.vm_queue import (
    select_existing_vm_record as _select_existing_vm_record,
)
from proxbox_api.services.sync.reconciliation.vm_queue import (
    unverifiable_vm_warnings,
)
from proxbox_api.services.sync.role_resolution import (
    RoleSnapshotDecision,
    apply_role_snapshot_policy,
    compute_role_snapshot_decision,
    persist_sync_state_with_role_compensation,
    resolve_snapshot_read_from_scan,
)
from proxbox_api.services.sync.stage_result import (
    WarningList,
    attach_skips_to_dict,
    attach_skips_to_list,
    require_no_dropped_vms,
    response_with_stage_warnings,
    result_warnings,
)
from proxbox_api.services.sync.storage_links import (
    build_storage_index,
    find_storage_record,
    storage_name_from_volume_id,
)
from proxbox_api.services.sync.sync_state_reader import (
    StaleEndpointAdoption,
    SyncStateVMResolution,
    adopt_vm_with_stale_endpoint_id,
    load_vm_last_synced_names,
    resolve_virtual_machine_by_sync_state,
    scan_vm_last_synced_role_ids,
)
from proxbox_api.services.sync.sync_state_writer import (
    reset_sidecar_availability_cache,
    write_virtual_disk_sync_state,
    write_virtual_machine_sync_state,
    write_vm_interface_sync_state,
)
from proxbox_api.services.sync.tag_resolver import resolve_proxmox_tag_ids
from proxbox_api.services.sync.task_history import sync_all_virtual_machine_task_histories
from proxbox_api.services.sync.virtual_machines import (
    build_netbox_virtual_machine_payload,
    build_virtual_machine_sync_state_fields,
)
from proxbox_api.services.sync.vm_cluster_guard import (
    filter_vm_records_in_cluster,
    log_cross_cluster_rejection,
    vm_record_in_cluster,
)
from proxbox_api.services.sync.vm_create import ensure_vm_type
from proxbox_api.services.sync.vm_filter import (
    SelectionMode,
    hydrate_vm_identities_from_sidecars,
    selection_skips,
)
from proxbox_api.services.sync.vm_filter import (
    filter_cluster_resources_by_netbox_vm_ids as _filter_selected_vm_resources,
)
from proxbox_api.services.sync.vm_filter import (
    filter_cluster_resources_for_selected_vm as _filter_selected_vm_resource,
)
from proxbox_api.services.sync.vm_helpers import (
    _compute_vm_patchable_fields,
    build_guest_mac_index,
    extract_vm_disk_aggregate_size,
    merged_guest_iface_from_mac_index,
    parse_proxmox_net_configs,
    parse_selected_netbox_vm_ids,
    preferred_primary_ip_order,
    resolve_netbox_cluster_id_by_name,
    stamp_vm_last_run_id,
)
from proxbox_api.services.sync.vm_helpers import (
    record_id as _record_id,
)
from proxbox_api.services.sync.vm_helpers import (
    relation_id as _relation_id,
)
from proxbox_api.services.sync.vm_helpers import (
    relation_name as _relation_name,
)
from proxbox_api.services.sync.vm_helpers import (
    to_mapping as _to_mapping,
)
from proxbox_api.services.sync.vmid_helpers import (
    extract_proxmox_endpoint_id,
    extract_proxmox_session_endpoint_id,
    extract_proxmox_vmid,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.session.proxmox_providers import load_configured_proxmox_endpoint_ids
from proxbox_api.utils import return_status_html
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

_BoundedInput = TypeVar("_BoundedInput")
_BoundedResult = TypeVar("_BoundedResult")
_UNSET_RESULT = object()


def _bounded_map_capacity(input_count: int, worker_count: int) -> tuple[int, int]:
    """Return effective worker and pending-queue limits for one bounded map."""

    workers = min(max(1, worker_count), input_count) if input_count else 0
    return workers, workers * 2


def _log_vm_full_update_terminal(
    *,
    outcome: str,
    total_ms: float,
    process_cpu_ms: float,
    snapshot_ms: float = 0.0,
    hydration_ms: float = 0.0,
    name_resolution_ms: float = 0.0,
    canonicalization_ms: float = 0.0,
    dispatch_ms: float = 0.0,
    persistence_ms: float = 0.0,
    fetch_requests: int = 0,
    fetch_latency_total_ms: float = 0.0,
    fetch_latency_max_ms: float = 0.0,
    fetch_workers: int = 0,
    fetch_queue_capacity: int = 0,
    peak_rss_kib: int | None = None,
) -> None:
    """Emit one terminal record for every full-update batch outcome."""

    effective_peak_rss_kib = (
        process_resource.getrusage(process_resource.RUSAGE_SELF).ru_maxrss
        if peak_rss_kib is None
        else peak_rss_kib
    )
    logger.info(
        "VM full-update terminal timing: outcome=%s total_ms=%.2f process_cpu_ms=%.2f "
        "snapshot_ms=%.2f hydration_ms=%.2f name_resolution_ms=%.2f "
        "canonicalization_ms=%.2f dispatch_ms=%.2f persistence_ms=%.2f "
        "fetch_requests=%d fetch_latency_total_ms=%.2f fetch_latency_max_ms=%.2f "
        "fetch_workers=%d fetch_queue_capacity=%d peak_rss_kib=%d",
        outcome,
        total_ms,
        process_cpu_ms,
        snapshot_ms,
        hydration_ms,
        name_resolution_ms,
        canonicalization_ms,
        dispatch_ms,
        persistence_ms,
        fetch_requests,
        fetch_latency_total_ms,
        fetch_latency_max_ms,
        fetch_workers,
        fetch_queue_capacity,
        effective_peak_rss_kib,
    )


def _vm_full_update_outcome(*, failed_count: int, prepared_count: int) -> str:
    """Classify the terminal batch outcome without inflating the orchestrator."""

    if prepared_count == 0:
        return "total_failure" if failed_count else "empty"
    return "partial_failure" if failed_count else "success"


@dataclass
class _VMFullUpdateTelemetry:
    """Mutable run-scoped measurements retained through cancellation."""

    started_at: float = field(default_factory=time.perf_counter)
    cpu_started_at: float = field(default_factory=time.process_time)
    fetch_latencies_ms: list[float] = field(default_factory=list)
    fetch_workers: int = 0
    fetch_queue_capacity: int = 0
    snapshot_ms: float = 0.0
    hydration_ms: float = 0.0
    name_resolution_ms: float = 0.0
    canonicalization_ms: float = 0.0
    dispatch_ms: float = 0.0
    persistence_ms: float = 0.0

    def emit(self, outcome: str) -> None:
        _log_vm_full_update_terminal(
            outcome=outcome,
            total_ms=(time.perf_counter() - self.started_at) * 1000,
            process_cpu_ms=(time.process_time() - self.cpu_started_at) * 1000,
            snapshot_ms=self.snapshot_ms,
            hydration_ms=self.hydration_ms,
            name_resolution_ms=self.name_resolution_ms,
            canonicalization_ms=self.canonicalization_ms,
            dispatch_ms=self.dispatch_ms,
            persistence_ms=self.persistence_ms,
            fetch_requests=len(self.fetch_latencies_ms),
            fetch_latency_total_ms=sum(self.fetch_latencies_ms),
            fetch_latency_max_ms=max(self.fetch_latencies_ms, default=0.0),
            fetch_workers=self.fetch_workers,
            fetch_queue_capacity=self.fetch_queue_capacity,
        )


async def _run_vm_full_update_with_cancellation_telemetry(
    callback: Callable[[], Awaitable[tuple[list[dict[str, object]], int]]],
    telemetry: _VMFullUpdateTelemetry,
) -> tuple[list[dict[str, object]], int]:
    """Run a full-update batch and emit its terminal cancellation outcome."""

    try:
        return await callback()
    except asyncio.CancelledError:
        telemetry.emit("cancelled")
        raise


async def _map_bounded_ordered(
    inputs: list[_BoundedInput],
    callback: Callable[[_BoundedInput], Awaitable[_BoundedResult]],
    *,
    worker_count: int,
) -> list[_BoundedResult | Exception]:
    """Map async work with fixed workers while retaining input order.

    The bounded queue limits pending inputs in addition to limiting active
    callbacks. Ordinary item failures are returned in their input positions;
    cancellation and other base exceptions propagate through the task group.
    """

    if not inputs:
        return []

    workers, queue_capacity = _bounded_map_capacity(len(inputs), worker_count)
    sentinel = object()
    queue: asyncio.Queue[tuple[int, _BoundedInput] | object] = asyncio.Queue(maxsize=queue_capacity)
    results: list[_BoundedResult | Exception | object] = [_UNSET_RESULT] * len(inputs)
    owner_task = asyncio.current_task()
    callback_cancellation: asyncio.CancelledError | None = None

    async def _worker() -> None:
        nonlocal callback_cancellation
        while True:
            item = await queue.get()
            try:
                if item is sentinel:
                    return
                index, value = cast("tuple[int, _BoundedInput]", item)
                try:
                    results[index] = await callback(value)
                except asyncio.CancelledError as exc:
                    if owner_task is not None and not owner_task.cancelling():
                        callback_cancellation = exc
                        owner_task.cancel()
                    raise
                except Exception as exc:
                    results[index] = exc
            finally:
                queue.task_done()

    try:
        async with asyncio.TaskGroup() as task_group:
            for _ in range(workers):
                task_group.create_task(_worker())
            for index, value in enumerate(inputs):
                await queue.put((index, value))
            for _ in range(workers):
                await queue.put(sentinel)
            await queue.join()
    except asyncio.CancelledError:
        if callback_cancellation is not None:
            raise callback_cancellation
        raise

    if any(result is _UNSET_RESULT for result in results):
        raise RuntimeError("Bounded async map completed without a result for every input")
    return cast("list[_BoundedResult | Exception]", results)


def _finalize_desired_vm_states(
    prepared_vms: list[_PreparedVMState],
) -> list[NetBoxVirtualMachineCreateBody]:
    """Validate final desired payloads after all in-run name mutations."""

    return [
        NetBoxVirtualMachineCreateBody.model_validate(prepared.desired_payload)
        for prepared in prepared_vms
    ]


def _validate_vm_inputs(
    vm_config: dict[str, object],
    resource: dict[str, object],
) -> tuple[ProxmoxVmConfigInput, ProxmoxVmResourceInput]:
    """Validate config then resource deterministically in one worker call."""

    return (
        ProxmoxVmConfigInput.model_validate(vm_config),
        ProxmoxVmResourceInput.model_validate(resource),
    )


router = APIRouter()


class SyncResultList(WarningList):
    """List result with optional sync warnings and a failed-item count.

    ``failed_count`` is the number of VMs that failed to reconcile in the stage.
    Callers such as the full-update orphan sweep use it to avoid treating a live VM
    that failed to sync as an orphan.
    """

    def __init__(
        self,
        values: list[dict] | None = None,
        *,
        warnings: list[dict[str, object]] | None = None,
        failed_count: int = 0,
    ) -> None:
        super().__init__(values, warnings=warnings)
        self.failed_count = failed_count


def _vm_stage_result(
    results: list,
    selection_skipped: list[dict[str, object]],
    *,
    failed_count: int = 0,
):
    """Return the VM stage result, telling the caller about VMs the selection dropped.

    A clean run keeps the historical list carrier; a run that dropped selected VMs
    returns them as warnings so a REST caller is not told everything synced. The
    failed-VM count survives either shape so the orphan sweep stays fail-closed.
    """
    carrier = SyncResultList(
        results,
        warnings=[*result_warnings(results), *selection_skipped],
        failed_count=failed_count,
    )
    wrapped = response_with_stage_warnings(carrier, result_key="virtual_machines")
    if isinstance(wrapped, dict) and failed_count:
        wrapped["failed_count"] = failed_count
    return wrapped


def _preserve_task_history_degradation(result: object, *, use_websocket: bool) -> None:
    if not isinstance(result, dict) or not result.get("degraded"):
        return
    if use_websocket:
        logger.warning(
            "VM task-history sync completed with degraded coverage: "
            "errors=%s reconciled=%s skipped=%s",
            result.get("errors", 0),
            result.get("created", 0),
            result.get("skipped", 0),
        )
        return
    raise ProxboxException(
        message="Task-history sync completed with degraded coverage",
        detail={
            "errors": result.get("errors", 0),
            "reconciled": result.get("created", 0),
            "skipped": result.get("skipped", 0),
        },
        http_status_code=502,
    )


def _stream_stage_payload(
    result: object,
    selection_skipped: list[dict[str, object]],
) -> dict[str, object]:
    """Build the SSE completion payload from the completed VM stage result.

    ``result`` is the stage wrapper: a plain list when clean, or a dict carrying
    ``virtual_machines``, ``warnings`` and ``degraded`` when degraded. Warnings
    raised while the stage ran (for example cluster verification skips) are kept
    alongside the earlier selection skips, without duplicates.
    """
    rows = result.get("virtual_machines", []) if isinstance(result, dict) else result
    stage_warnings = result_warnings(result)
    extra = [item for item in selection_skipped if item not in stage_warnings]
    return attach_skips_to_dict({"count": len(rows)}, [*stage_warnings, *extra])


@dataclass(slots=True)
class _VMPreparationContext:
    """Run-scoped dependencies for preparing a VM from a fetched Proxmox config."""

    nb: object
    tag: object
    overwrite_flags: SyncOverwriteFlags
    behavior_flags: SyncBehaviorFlags
    effective_vm_overwrite_flags: SyncOverwriteFlags
    cluster_dependency_cache: dict[object, dict[str, object]]
    node_device_cache: dict[tuple[int | None, str, str], object]
    vm_role_cache: dict[str, object]
    vm_role_mapping: dict[str, dict[str, object]]
    tag_refs: list[dict[str, object]]
    proxmox_url_by_cluster: dict[object, str]
    endpoint_id_by_cluster: dict[str, int]
    resolve_vm_type: Callable[[str], Awaitable[object | None]]
    resolve_vm_proxmox_tag_ids: Callable[[str, dict[str, object]], Awaitable[list[int]]]
    # Both default so an existing constructor keeps working -- these are optional
    # accelerators, not new required inputs, and a caller that omits them simply gets
    # the ostype-derived platform and an uncached lookup.
    #
    # Only consulted for the opt-in guest-agent platform refinement. Absent entries are
    # normal and simply mean the refinement is skipped for that cluster.
    px_by_cluster: dict[object, object] = field(default_factory=dict)
    # Run-scoped {platform name: id or None}. An estate's VMs share a handful of
    # operating systems, so without this the sync reconciles a platform per VM.
    platform_cache: dict[str, int | None] = field(default_factory=dict)


def _node_naming_kwargs(source: object, cluster_name: str) -> dict[str, object]:
    """Return normalized node naming inputs for a Proxmox endpoint or cluster."""

    return {
        "cluster_name": cluster_name,
        "endpoint_name": str(getattr(source, "endpoint_name", "") or cluster_name),
        "node_device_name_template": str(
            getattr(source, "node_device_name_template", "{node}") or "{node}"
        ),
        "endpoint_id": extract_proxmox_session_endpoint_id(source),
    }


def _endpoint_node_naming_kwargs(
    source: object,
    cluster_name: str,
    endpoint_id: int | None,
) -> dict[str, object]:
    naming = _node_naming_kwargs(source, cluster_name)
    naming["endpoint_id"] = endpoint_id
    return naming


def _bridge_device_lookup_args(
    info: dict[str, object],
) -> tuple[str, str, int | None, int | None]:
    """Extract normalized node-device lookup inputs from interface metadata."""

    site_id = info.get("site_id")
    return (
        str(info.get("cluster_name", "") or ""),
        str(info.get("resource_node", "") or ""),
        site_id if isinstance(site_id, int) else None,
        extract_proxmox_endpoint_id(info),
    )


def _select_bridge_cluster_status(
    cluster_status: object,
    *,
    cluster_name: str,
    endpoint_id: int | None,
) -> object | None:
    candidates = [
        item
        for item in cast("list[object]", cluster_status)
        if str(getattr(item, "name", "") or "") == cluster_name
    ]
    if endpoint_id is not None:
        endpoint_matches = [
            item for item in candidates if extract_proxmox_session_endpoint_id(item) == endpoint_id
        ]
        if len(endpoint_matches) == 1:
            return endpoint_matches[0]
        if endpoint_matches:
            raise ProxboxException(
                message="Ambiguous Proxmox bridge endpoint status",
                detail=(
                    f"Multiple status records match endpoint {endpoint_id} and "
                    f"cluster {cluster_name!r}."
                ),
            )
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise ProxboxException(
            message="Ambiguous Proxmox bridge endpoint status",
            detail=(
                f"Endpoint {endpoint_id!r} cannot uniquely select cluster status "
                f"for {cluster_name!r}."
            ),
        )
    return None


async def _resolve_bridge_device_id(
    nb: object,
    cache: dict[tuple[int | None, str, str], int | None],
    cluster_status: object,
    cluster_name: str,
    node_name: str,
    site_id: int | None,
    endpoint_id: int | None,
) -> int | None:
    """Resolve and cache the NetBox node device used by a VM bridge."""

    from proxbox_api.netbox_rest import rest_list_async

    cache_key = (endpoint_id, cluster_name, node_name)
    if cache_key in cache:
        return cache[cache_key]
    status = _select_bridge_cluster_status(
        cluster_status,
        cluster_name=cluster_name,
        endpoint_id=endpoint_id,
    )
    naming = _node_naming_kwargs(status, cluster_name)
    try:
        device_name = render_node_device_name(
            node_name,
            naming["cluster_name"],
            naming["endpoint_name"],
            naming["node_device_name_template"],
        )
    except NodeDeviceNameError:
        cache[cache_key] = None
        return None
    sidecar_query: dict[str, object] = {
        "proxmox_cluster_name": cluster_name,
        "proxmox_node_name": node_name,
        "limit": 3,
    }
    if endpoint_id is not None:
        sidecar_query["proxmox_endpoint_raw_id"] = endpoint_id
    try:
        sidecars = await rest_list_async(
            nb, "/api/plugins/proxbox/sync-state/devices/", query=sidecar_query
        )
    except Exception:
        sidecars = []
    if endpoint_id is not None:
        sidecars = [
            row for row in sidecars if extract_proxmox_endpoint_id(row) in {None, endpoint_id}
        ]
    elif (
        len(
            {
                claimed_id
                for row in sidecars
                if (claimed_id := extract_proxmox_endpoint_id(row)) is not None
            }
        )
        > 1
    ):
        raise ProxboxException(
            message="Ambiguous Proxmox bridge device identity",
            detail=f"Multiple endpoints claim node {node_name!r} in cluster {cluster_name!r}.",
        )
    sidecar_device_ids = {
        device_id for row in sidecars if (device_id := _relation_id(row.get("device"))) is not None
    }
    if len(sidecar_device_ids) > 1:
        raise ProxboxException(
            message="Ambiguous Proxmox bridge device identity",
            detail=f"Multiple devices claim node {node_name!r} in cluster {cluster_name!r}.",
        )
    if sidecar_device_ids:
        device_id = next(iter(sidecar_device_ids))
        cache[cache_key] = device_id
        return device_id

    query: dict[str, object] = {"name": device_name, "limit": 3}
    if site_id is not None:
        query["site_id"] = site_id
    try:
        device_records = await rest_list_async(nb, "/api/dcim/devices/", query=query)
        device_ids = {
            device_id
            for record in device_records
            if (device_id := _relation_id(record.get("id"))) is not None
        }
        if len(device_ids) > 1:
            raise ProxboxException(
                message="Ambiguous Proxmox bridge device identity",
                detail=f"Multiple devices match rendered node name {device_name!r}.",
            )
        device_id = next(iter(device_ids), None)
    except ProxboxException:
        raise
    except Exception:
        device_id = None
    cache[cache_key] = device_id
    return device_id


async def _resolve_vm_platform_id(
    nb: object,
    *,
    px_session: object | None,
    node: str,
    vmid: int,
    resource: dict[str, object],
    vm_config: dict[str, object],
    tag_refs: list[dict[str, object]],
    refine_from_guest_agent: bool,
    cache: dict[str, int | None] | None = None,
) -> int | None:
    """Resolve the NetBox platform id for one VM's guest operating system.

    Tier 1 always runs and costs nothing extra: ``ostype`` is already in the config
    payload the sync fetched.

    Tier 2 is the opt-in guest-agent refinement. It is attempted only when the caller
    enabled it **and** the guest can plausibly answer -- QEMU, running, and already known
    to have the agent enabled, all of which the sync knows without asking. That gating is
    what keeps the worst case a bounded per-VM cost the operator opted into rather than an
    open-ended fan-out across the estate.

    Returns ``None`` when the guest OS cannot be identified, leaving the field unset.
    """
    from proxbox_api.services.sync.vm_create import ensure_vm_platform

    ostype = vm_config.get("ostype") if isinstance(vm_config, dict) else None

    osinfo: object = None
    if refine_from_guest_agent and px_session is not None:
        vm_type = str(resource.get("type") or "").strip().lower()
        status = str(resource.get("status") or "").strip().lower()
        agent_enabled = _parse_proxmox_kv_flag(vm_config.get("agent"))
        if vm_type == "qemu" and status == "running" and agent_enabled:
            from proxbox_api.services.proxmox_helpers import get_qemu_guest_agent_osinfo

            try:
                osinfo = await get_qemu_guest_agent_osinfo(px_session, node, vmid)
            except Exception as error:  # noqa: BLE001
                # The helper already degrades internally; this is belt-and-braces so a
                # future change there cannot turn a nicety into a failed VM sync.
                logger.debug(
                    "Guest-agent osinfo unavailable for node=%s vmid=%s: %s",
                    node,
                    vmid,
                    error,
                )
                osinfo = None

    return await ensure_vm_platform(
        nb,
        ostype=ostype,
        guest_agent_osinfo=osinfo,
        tag_refs=tag_refs,
        cache=cache,
    )


def _missing_vm_lookup() -> dict[str, object]:
    """Return an impossible lookup after typed sidecar resolution found no VM."""
    return {"id": 0}


def _endpoint_id_by_cluster_names(
    pxs: object,
    cluster_status: list[object] | None,
) -> dict[str, int]:
    """Map observed cluster/node names to the Proxmox endpoint id for the run."""
    endpoint_id_by_cluster: dict[str, int] = {}
    for px, cluster in zip(list(pxs or []), cluster_status or []):
        endpoint_id = extract_proxmox_session_endpoint_id(px)
        if endpoint_id is None:
            continue
        names = {
            getattr(cluster, "name", None),
            getattr(px, "name", None),
            getattr(px, "cluster_name", None),
            getattr(px, "node_name", None),
        }
        for name in names:
            name_text = str(name or "").strip()
            if name_text:
                endpoint_id_by_cluster[name_text] = endpoint_id
    return endpoint_id_by_cluster


def _build_vm_endpoint_contexts(
    pxs: object,
    cluster_status: object,
) -> tuple[
    list[object],
    list[object],
    dict[str, int],
    dict[object, str],
    dict[object, object],
]:
    px_list = list(pxs or [])
    status_list = list(cluster_status or [])
    endpoint_id_by_cluster = _endpoint_id_by_cluster_names(px_list, status_list)
    proxmox_url_by_cluster: dict[object, str] = {}
    px_by_cluster: dict[object, object] = {}
    for px, status in zip(px_list, status_list):
        cluster_name = getattr(status, "name", None) or getattr(px, "cluster_name", None)
        endpoint_id = extract_proxmox_session_endpoint_id(px)
        cluster_key = (endpoint_id, str(cluster_name or ""))
        px_domain = getattr(px, "domain", None) or getattr(px, "ip_address", None) or ""
        px_port = getattr(px, "http_port", 8006)
        if cluster_name and px_domain:
            url = f"https://{px_domain}:{px_port}"
            proxmox_url_by_cluster[cluster_key] = url
            proxmox_url_by_cluster.setdefault(str(cluster_name), url)
        px_by_cluster[cluster_key] = px
        for candidate_name in {
            cluster_name,
            getattr(px, "name", None),
            getattr(px, "cluster_name", None),
            getattr(px, "node_name", None),
        }:
            name_text = str(candidate_name or "").strip()
            if name_text:
                px_by_cluster.setdefault(name_text, px)
    return (
        px_list,
        status_list,
        endpoint_id_by_cluster,
        proxmox_url_by_cluster,
        px_by_cluster,
    )


def _session_endpoint_id_at(pxs: object, index: int) -> int | None:
    """Return one run-ordered endpoint id without requiring a sized dependency."""
    try:
        return extract_proxmox_session_endpoint_id(list(pxs or [])[index])
    except IndexError:
        return None


def _cluster_dependency_site_id(cluster_dependencies: dict[str, object]) -> int | None:
    cached_site_id = cluster_dependencies.get("site_id")
    try:
        if cached_site_id is not None and int(cached_site_id) > 0:
            return int(cached_site_id)
    except (TypeError, ValueError):
        pass
    return _effective_cluster_site_id(
        cluster_dependencies.get("cluster"),
        fallback_site_id=getattr(cluster_dependencies.get("site"), "id", None),
    )


async def _fetch_vm_config_only(*, pxs: object, resource: dict[str, object]) -> dict[str, object]:
    """Fetch raw Proxmox VM config without doing CPU or NetBox work."""

    vm_type = str(resource.get("type") or "unknown")
    vm_config_result = get_vm_config(
        pxs=pxs,
        node=resource.get("node"),
        type=vm_type,
        vmid=resource.get("vmid"),
    )
    if inspect.isawaitable(vm_config_result):
        vm_config_result = await vm_config_result
    return cast("dict[str, object]", vm_config_result or {})


async def _prepare_vm_from_config(  # noqa: C901
    cluster_name: str,
    resource: dict[str, object],
    vm_config: dict[str, object],
    context: _VMPreparationContext,
    *,
    endpoint_id: int | None = None,
    cluster_source: object | None = None,
    px_source: object | None = None,
) -> _PreparedVMState:
    """Prepare desired NetBox VM state from an already fetched Proxmox config."""

    vm_type = str(resource.get("type") or "unknown")
    vm_type_key = vm_type.lower() if vm_type else "undefined"
    if vm_type_key not in context.vm_role_mapping:
        vm_type_key = "undefined"

    vm_config_obj, resource_obj = await asyncio.to_thread(
        _validate_vm_inputs,
        vm_config,
        resource,
    )

    dependency_key = (endpoint_id, str(cluster_name))
    cluster_dependencies = context.cluster_dependency_cache.get(
        dependency_key,
        context.cluster_dependency_cache.get(str(cluster_name), {}),
    )
    cluster = cluster_dependencies.get("cluster")
    if cluster is None:
        raise ProxboxException(
            message="Error creating Virtual Machine dependent objects (cluster, device, tag and role)",
            python_exception=f"Missing precomputed cluster dependency for cluster={cluster_name}",
        )

    node_name = str(resource.get("node"))
    site_id = _cluster_dependency_site_id(cluster_dependencies)
    endpoint_id = endpoint_id or context.endpoint_id_by_cluster.get(str(cluster_name))
    device_key = (endpoint_id, str(cluster_name), node_name)
    device = context.node_device_cache.get(device_key)
    if device is None:
        device = await _ensure_device(
            context.nb,
            device_name=node_name,
            **_endpoint_node_naming_kwargs(
                cluster_source or px_source or context.px_by_cluster.get(dependency_key),
                cluster_name,
                endpoint_id,
            ),
            cluster_id=getattr(cluster, "id", None),
            device_type_id=getattr(cluster_dependencies.get("device_type"), "id", None),
            role_id=getattr(cluster_dependencies.get("device_role"), "id", None),
            site_id=site_id,
            tag_refs=context.tag_refs,
            overwrite_device_role=context.overwrite_flags.overwrite_device_role,
            overwrite_device_type=context.overwrite_flags.overwrite_device_type,
            overwrite_device_tags=context.overwrite_flags.overwrite_device_tags,
            overwrite_flags=context.overwrite_flags,
        )
        context.node_device_cache[device_key] = device

    role = context.vm_role_cache.get(vm_type_key)
    if role is None:
        role_payload = context.vm_role_mapping.get(
            vm_type_key, context.vm_role_mapping["undefined"]
        )
        role = await rest_reconcile_async(
            context.nb,
            "/api/dcim/device-roles/",
            lookup={"slug": role_payload.get("slug")},
            payload={
                **role_payload,
                "tags": context.tag_refs,
            },
            schema=NetBoxDeviceRoleSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "color": record.get("color"),
                "description": record.get("description"),
                "vm_role": record.get("vm_role"),
                "tags": record.get("tags"),
            },
        )
        context.vm_role_cache[vm_type_key] = role

    vm_type_obj = await context.resolve_vm_type(vm_type_key)
    vm_type_id = int(getattr(vm_type_obj, "id", 0) or 0) if vm_type_obj else None

    now = datetime.now(timezone.utc)
    proxmox_tag_ids = await context.resolve_vm_proxmox_tag_ids(str(cluster_name), vm_config)
    proxbox_tag_id = int(getattr(context.tag, "id", 0) or 0)
    merged_tag_ids = sorted({proxbox_tag_id, *proxmox_tag_ids} - {0})
    platform_id = await _resolve_vm_platform_id(
        context.nb,
        px_session=px_source or context.px_by_cluster.get(dependency_key),
        node=str(resource.get("node") or ""),
        vmid=int(resource.get("vmid") or 0),
        resource=resource,
        vm_config=vm_config if isinstance(vm_config, dict) else {},
        tag_refs=context.tag_refs,
        refine_from_guest_agent=bool(context.behavior_flags.sync_vm_platform_from_guest_agent),
        cache=context.platform_cache,
    )

    desired_payload = await asyncio.to_thread(
        build_netbox_virtual_machine_payload,
        proxmox_resource=resource_obj,
        proxmox_config=vm_config_obj,
        cluster_id=int(getattr(cluster, "id", 0) or 0),
        device_id=int(getattr(device, "id", 0) or 0),
        role_id=None if vm_type_id else int(getattr(role, "id", 0) or 0),
        tag_ids=merged_tag_ids,
        site_id=site_id,
        tenant_id=int(getattr(cluster_dependencies.get("tenant"), "id", 0) or 0) or None,
        virtual_machine_type_id=vm_type_id,
        platform_id=platform_id,
        parse_description_metadata=context.behavior_flags.parse_description_metadata,
        overwrite_flags=context.effective_vm_overwrite_flags,
    )
    lookup = _missing_vm_lookup()
    sync_state_fields = build_virtual_machine_sync_state_fields(
        proxmox_resource=resource_obj,
        proxmox_config=vm_config_obj,
        last_updated=now,
        cluster_name=str(cluster_name),
        proxmox_url=(
            context.proxmox_url_by_cluster.get(dependency_key)
            or context.proxmox_url_by_cluster.get(str(cluster_name))
        ),
        endpoint_id=endpoint_id,
    )

    return _PreparedVMState(
        cluster_name=str(cluster_name),
        resource=resource,
        vm_config=vm_config,
        vm_config_obj=vm_config_obj,
        desired_payload=desired_payload,
        lookup=lookup,
        now=now,
        vm_type=vm_type,
        sync_state_fields=sync_state_fields,
    )


def _vm_dependency_error_detail(error: Exception) -> str:
    details: list[str] = []
    for attr_name in ("detail", "python_exception"):
        value = getattr(error, attr_name, None)
        if value is not None:
            text = str(value)
            if text and text not in details:
                details.append(text)
    if not details:
        details.append(str(error))
    return " | ".join(details)


async def _resolve_vm_dns_name(
    *,
    proxmox_session: object | None,
    node: str | None,
    vmid: object,
    vm_type: object,
    vm_config: dict[str, object] | None,
) -> str | None:
    """Resolve the guest hostname to use as IPAM `dns_name` for a VM.

    LXC: read `hostname` from VM config (already in `vm_config`).
    QEMU: query the guest agent via `get_qemu_guest_agent_hostname`.
    Returns a sanitized hostname or None when unavailable.
    """
    if vm_type == "lxc":
        if isinstance(vm_config, dict):
            return sanitize_dns_hostname(vm_config.get("hostname"))
        return None

    if vm_type != "qemu" or proxmox_session is None or not node or vmid is None:
        return None

    if isinstance(vm_config, dict) and not _parse_proxmox_kv_flag(vm_config.get("agent")):
        return None

    try:
        return await get_qemu_guest_agent_hostname(proxmox_session, node, int(vmid))
    except Exception as exc:
        logger.debug("VM dns_name resolution failed for node=%s vmid=%s: %s", node, vmid, exc)
        return None


async def _load_netbox_virtual_machine_snapshot(
    nb: object,
    *,
    fresh: bool = False,
) -> list[dict[str, object]]:
    """Fetch all NetBox virtual machines once and keep them in-memory for comparison."""

    if fresh:
        clear_rest_get_cache_for_path(nb, "/api/virtualization/virtual-machines/")

    records = await rest_list_paginated_async(
        nb,
        "/api/virtualization/virtual-machines/",
        page_size=200,
    )
    snapshot = [serialized for record in records if (serialized := _to_mapping(record))]
    await _complete_vm_snapshot_clusters(nb, snapshot)
    return snapshot


_VM_CLUSTER_COMPLETION_CONCURRENCY = 8


async def _read_vm_cluster_field(
    nb: object,
    record: dict[str, object],
    semaphore: asyncio.Semaphore,
) -> None:
    """Re-read one NetBox VM by id and copy its ``cluster`` field, tolerating failures."""
    record_id = _relation_id(record.get("id"))
    async with semaphore:
        try:
            fetched = _to_mapping(
                await rest_first_async(
                    nb,
                    "/api/virtualization/virtual-machines/",
                    query={"id": record_id, "limit": 2},
                )
            )
        except Exception as exc:  # noqa: BLE001 - best-effort completion, guard skips the row
            logger.debug("Could not complete cluster for NetBox VM id=%s: %s", record_id, exc)
            return
    if fetched and "cluster" in fetched:
        record["cluster"] = fetched["cluster"]


async def _complete_vm_snapshot_clusters(
    nb: object,
    snapshot: list[dict[str, object]],
    *,
    concurrency: int = _VM_CLUSTER_COMPLETION_CONCURRENCY,
) -> int:
    """Fill ``cluster`` on every snapshot row that lacks the field.

    Real NetBox list rows always carry ``cluster`` (an explicit ``null`` is a
    real unassigned VM and is left alone), so this only triggers for anomalous
    or trimmed rows. Every such row is re-read by id, with at most
    ``concurrency`` reads in flight, so no row is starved by an arbitrary
    allowance. A failed read leaves the row incomplete; the queue then skips
    the affected VM and reports it as a stage warning.
    """
    pending = [
        record
        for record in snapshot
        if "cluster" not in record and _relation_id(record.get("id")) is not None
    ]
    semaphore = asyncio.Semaphore(max(1, concurrency))
    await asyncio.gather(*(_read_vm_cluster_field(nb, record, semaphore) for record in pending))
    return len(pending)


def _prepared_proxmox_vmid(prepared: _PreparedVMState) -> int | None:
    vmid = _relation_id(prepared.resource.get("vmid"))
    return vmid if vmid is not None and vmid > 0 else None


def _overlay_sidecar_identity_on_vm_snapshot(
    record: dict[str, object],
    *,
    prepared: _PreparedVMState,
    proxmox_vmid: int,
    endpoint_id: int | None,
) -> None:
    record["proxmox_vm_id"] = proxmox_vmid
    if endpoint_id is not None:
        record["proxmox_endpoint_id"] = endpoint_id
    vm_type = str(prepared.vm_type or "").strip().lower()
    if vm_type in {"qemu", "lxc"}:
        record["proxmox_vm_type"] = vm_type


def _active_session_endpoint_ids(sessions: object) -> frozenset[int]:
    """Return the endpoint ids of every Proxmox session active in this run."""
    return frozenset(
        endpoint_id
        for session in list(sessions or [])
        if (endpoint_id := extract_proxmox_session_endpoint_id(session)) is not None
    )


_ConfiguredEndpointLoader = Callable[[], Awaitable[frozenset[int] | None]]


class _ConfiguredEndpointIds:
    """Lazily load, once per run, every configured Proxmox endpoint id.

    Resolves to ``None`` when the inventory cannot be loaded so callers fail
    closed. The sessions of this run are always included.
    """

    def __init__(self, nb: object, sessions: object) -> None:
        self._nb = nb
        self._active = _active_session_endpoint_ids(sessions)
        self._loaded = False
        self._ids: frozenset[int] | None = None

    async def __call__(self) -> frozenset[int] | None:
        if not self._loaded:
            inventory = await load_configured_proxmox_endpoint_ids(self._nb)
            self._ids = None if inventory is None else inventory | self._active
            self._loaded = True
        return self._ids


async def _resolve_vm_sidecar_identity(
    nb: object,
    *,
    prepared: _PreparedVMState,
    proxmox_vmid: int,
    endpoint_id: int | None,
    cluster_id: int | None,
    configured_endpoint_ids: _ConfiguredEndpointLoader | None,
) -> SyncStateVMResolution | None:
    """Resolve a prepared VM's sidecar identity, self-healing a stale endpoint id.

    Self-heal needs the live endpoint, the NetBox cluster and a loader for the
    configured endpoint inventory; without all three the plain endpoint-keyed
    lookup runs. An inventory that cannot be loaded disables adoption.
    """
    if endpoint_id is None or cluster_id is None or configured_endpoint_ids is None:
        return await resolve_virtual_machine_by_sync_state(
            nb,
            proxmox_vm_id=proxmox_vmid,
            endpoint_id=endpoint_id,
            cluster_id=cluster_id,
        )
    try:
        # Ambiguous or unverifiable identity raises: only a lookup that proved the
        # VM absent may fall through to the stale-id adoption.
        resolution = await resolve_virtual_machine_by_sync_state(
            nb,
            proxmox_vm_id=proxmox_vmid,
            endpoint_id=endpoint_id,
            cluster_id=cluster_id,
            fail_on_ambiguous=True,
        )
    except ProxboxException:
        return None
    if resolution is not None:
        return resolution
    configured = await configured_endpoint_ids()
    if configured is None:
        return None
    return await adopt_vm_with_stale_endpoint_id(
        nb,
        proxmox_vm_id=proxmox_vmid,
        endpoint_id=endpoint_id,
        cluster_id=cluster_id,
        adoption=StaleEndpointAdoption(
            vm_type=str(prepared.vm_type or ""),
            cluster_name=prepared.cluster_name,
            vm_name=str(prepared.resource.get("name") or ""),
            configured_endpoint_ids=configured,
        ),
    )


async def _hydrate_vm_snapshot_with_sidecar_identity(
    nb: object,
    *,
    prepared_vms: list[_PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
    configured_endpoint_ids: _ConfiguredEndpointLoader | None = None,
) -> int:
    """Overlay typed sidecar VM identity onto snapshot records.

    The reconciliation queue is intentionally pure and indexes the loaded VM
    snapshot. Before building that queue, use the sidecar resolver for prepared
    VMs that are not already indexed so owned rows are adopted instead of
    treated as name collisions or creates. ``configured_endpoint_ids`` loads the
    ids of every configured endpoint; when supplied, a VM stored under an
    endpoint id that no configured endpoint owns (and whose name matches) is
    re-bound to the live endpoint instead of being duplicated.
    """
    if not prepared_vms:
        return 0

    (
        endpoint_typed_vm_index,
        endpoint_untyped_vm_candidates,
        cluster_typed_vm_index,
        cluster_untyped_vm_candidates,
    ) = _build_vm_snapshot_identity_indexes(netbox_snapshot)
    snapshot_by_id = {
        record_id: record
        for record in netbox_snapshot
        if (record_id := _relation_id(record.get("id"))) is not None
    }
    resolved_keys: set[tuple[int | None, int | None, int, str]] = set()
    hydrated = 0

    for prepared in prepared_vms:
        proxmox_vmid = _prepared_proxmox_vmid(prepared)
        if proxmox_vmid is None:
            continue
        endpoint_id = extract_proxmox_endpoint_id(prepared.sync_state_fields)
        cluster_id = _relation_id(prepared.desired_payload.get("cluster"))
        if (
            _select_existing_vm_record(
                prepared=prepared,
                endpoint_id=endpoint_id,
                cluster_id=cluster_id,
                proxmox_vmid=proxmox_vmid,
                endpoint_typed_index=endpoint_typed_vm_index,
                endpoint_untyped_candidates=endpoint_untyped_vm_candidates,
                cluster_typed_index=cluster_typed_vm_index,
                cluster_untyped_candidates=cluster_untyped_vm_candidates,
            )
            is not None
        ):
            continue

        vm_type = str(prepared.vm_type or "").strip().lower()
        resolver_key = (endpoint_id, cluster_id, proxmox_vmid, vm_type)
        if resolver_key in resolved_keys:
            continue
        resolved_keys.add(resolver_key)

        resolution = await _resolve_vm_sidecar_identity(
            nb,
            prepared=prepared,
            proxmox_vmid=proxmox_vmid,
            endpoint_id=endpoint_id,
            cluster_id=cluster_id,
            configured_endpoint_ids=configured_endpoint_ids,
        )
        if resolution is None or resolution.source != "sidecar":
            continue

        record = snapshot_by_id.get(resolution.record_id)
        if record is None:
            record = _to_mapping(resolution.record)
            if not record:
                continue
            netbox_snapshot.append(record)
            snapshot_by_id[resolution.record_id] = record
        _overlay_sidecar_identity_on_vm_snapshot(
            record,
            prepared=prepared,
            proxmox_vmid=proxmox_vmid,
            endpoint_id=endpoint_id,
        )
        hydrated += 1

    if hydrated:
        logger.info(
            "Hydrated %d VM snapshot records from Proxbox sync-state sidecar identity",
            hydrated,
        )
    return hydrated


def _build_vm_index_by_proxmox_id(
    snapshot: list[dict[str, object]],
) -> dict[tuple[int, int], dict[str, object]]:
    """Index a VM snapshot by ``(proxmox_endpoint_id, proxmox_vm_id)``."""
    index: dict[tuple[int, int], dict[str, object]] = {}
    for vm in snapshot:
        endpoint_id = extract_proxmox_endpoint_id(vm)
        if endpoint_id is None:
            continue
        vmid = _extract_proxmox_vmid_from_vm_record(vm)
        if vmid is None:
            continue
        index.setdefault((endpoint_id, vmid), vm)
    return index


def _extract_proxmox_vmid_from_vm_record(vm: dict[str, object]) -> int | None:
    """Extract a positive VMID from hydrated typed sync state."""
    try:
        vmid = int(str(extract_proxmox_vmid(vm) or "").strip())
    except (TypeError, ValueError):
        return None
    return vmid if vmid > 0 else None


def _build_vm_candidates_by_proxmox_id(
    snapshot: list[dict[str, object]],
) -> dict[int, list[dict[str, object]]]:
    """Index VM snapshot candidates by vmid for unambiguous fallback lookup."""
    candidates: dict[int, list[dict[str, object]]] = {}
    for vm in snapshot:
        vmid = _extract_proxmox_vmid_from_vm_record(vm)
        if vmid is not None:
            candidates.setdefault(vmid, []).append(vm)
    return candidates


def _select_unique_vm_candidate_by_vmid(
    candidates_by_vmid: dict[int, list[dict[str, object]]],
    *,
    vmid: int,
    cluster_name: str,
    sync_context: str,
) -> dict[str, object] | None:
    """Return the only vmid candidate, refusing cross-cluster ambiguity."""
    candidates = candidates_by_vmid.get(vmid, [])
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.warning(
            "Skipping VM %s sync for cluster=%s vmid=%s: ambiguous vmid across clusters",
            sync_context,
            cluster_name or "unknown",
            vmid,
        )
    return None


def _scope_vm_lookup_to_cluster(
    vm_index: dict[tuple[int, int], dict[str, object]],
    candidates_by_vmid: dict[int, list[dict[str, object]]],
    *,
    vmid: int,
    endpoint_id: int | None,
    cluster_id: int | None,
    cluster_name: str,
    sync_context: str,
) -> tuple[dict[tuple[int, int], dict[str, object]], dict[int, list[dict[str, object]]]]:
    """Restrict the endpoint/vmid lookup views to VMs of the cluster being synced.

    ``(endpoint id, vmid)`` is not unique across clusters, so a match from
    another cluster is logged and dropped, and the live cluster's own VM (which
    the first-wins index may have shadowed) stays reachable. Without a known
    live cluster the views are returned unchanged.
    """
    if cluster_id is None and not cluster_name:
        return vm_index, candidates_by_vmid
    candidates = candidates_by_vmid.get(vmid, [])
    in_cluster = filter_vm_records_in_cluster(
        candidates, cluster_id=cluster_id, cluster_name=cluster_name
    )
    kept_ids = {id(record) for record in in_cluster}
    for record in candidates:
        if id(record) not in kept_ids and extract_proxmox_endpoint_id(record) == endpoint_id:
            log_cross_cluster_rejection(
                record,
                vmid=vmid,
                endpoint_id=endpoint_id,
                cluster_id=cluster_id,
                cluster_name=cluster_name,
                context=sync_context,
            )
    scoped_index: dict[tuple[int, int], dict[str, object]] = {}
    indexed = vm_index.get((endpoint_id, vmid)) if endpoint_id is not None else None
    if indexed is not None and vm_record_in_cluster(
        indexed, cluster_id=cluster_id, cluster_name=cluster_name
    ):
        scoped_index[(endpoint_id, vmid)] = indexed
    for record in in_cluster:
        record_endpoint_id = extract_proxmox_endpoint_id(record)
        if record_endpoint_id is not None:
            scoped_index.setdefault((record_endpoint_id, vmid), record)
    return scoped_index, {vmid: in_cluster}


def _resolve_vm_from_index_or_unique_vmid(
    vm_index: dict[tuple[int, int], dict[str, object]],
    candidates_by_vmid: dict[int, list[dict[str, object]]],
    *,
    endpoint_id: int | None,
    raw_vmid: object,
    cluster_name: str,
    sync_context: str,
    cluster_id: int | None = None,
) -> dict[str, object] | None:
    """Resolve a VM by endpoint-scoped key, with legacy fallback only when safe.

    The match must live in the cluster being synced (``cluster_id`` and/or
    ``cluster_name``); a same-key VM from another cluster is never returned.
    """
    try:
        vmid = int(str(raw_vmid).strip())
    except (TypeError, ValueError):
        return None

    vm_index, candidates_by_vmid = _scope_vm_lookup_to_cluster(
        vm_index,
        candidates_by_vmid,
        vmid=vmid,
        endpoint_id=endpoint_id,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        sync_context=sync_context,
    )

    if endpoint_id is not None:
        scoped_vm = vm_index.get((endpoint_id, vmid))
        if scoped_vm is not None:
            return scoped_vm

        legacy_candidates = [
            candidate
            for candidate in candidates_by_vmid.get(vmid, [])
            if extract_proxmox_endpoint_id(candidate) is None
        ]
        if len(legacy_candidates) == 1:
            return legacy_candidates[0]
        if legacy_candidates or candidates_by_vmid.get(vmid):
            logger.warning(
                "Skipping VM %s sync for endpoint_id=%s cluster=%s vmid=%s: "
                "no unambiguous endpoint-scoped NetBox VM match",
                sync_context,
                endpoint_id,
                cluster_name or "unknown",
                vmid,
            )
        return None

    return _select_unique_vm_candidate_by_vmid(
        candidates_by_vmid,
        vmid=vmid,
        cluster_name=cluster_name,
        sync_context=sync_context,
    )


def _build_cluster_id_cache_from_vm_snapshot(
    snapshot: list[dict[str, object]],
) -> dict[str, int | None]:
    """Seed a cluster-name cache from VM snapshot relation data."""
    cache: dict[str, int | None] = {}
    for vm in snapshot:
        cluster = vm.get("cluster")
        cluster_id = _relation_id(cluster)
        cluster_name = _relation_name(cluster)
        if cluster_id is None or not cluster_name:
            continue
        cache[str(cluster_name).strip().casefold()] = cluster_id
    return cache


def _resolve_vm_overwrites(
    role: bool | None,
    vm_type: bool | None,
    tags: bool | None,
    description: bool | None,
    custom_fields_overwrite: bool | None,
    overwrite_flags: SyncOverwriteFlags,
) -> tuple[bool, bool, bool, bool, bool]:
    """Resolve VM-scalar overwrite gates from flat Query params + `overwrite_flags`.

    Flat params (`overwrite_vm_role`, `overwrite_vm_type`, `overwrite_vm_tags`,
    `overwrite_vm_description`, `overwrite_vm_custom_fields`) win when explicitly supplied (`True`/`False`);
    `None` means "not provided" and the corresponding field on `overwrite_flags`
    is used instead. Old clients that only set the flat params keep the original
    semantics; new clients can drive everything through `overwrite_flags`.
    """
    return (
        role if role is not None else overwrite_flags.overwrite_vm_role,
        vm_type if vm_type is not None else overwrite_flags.overwrite_vm_type,
        tags if tags is not None else overwrite_flags.overwrite_vm_tags,
        description if description is not None else overwrite_flags.overwrite_vm_description,
        custom_fields_overwrite
        if custom_fields_overwrite is not None
        else overwrite_flags.overwrite_vm_custom_fields,
    )


@dataclass(slots=True)
class _VMNameIndexes:
    endpoint_typed: dict
    endpoint_untyped: dict
    cluster_typed: dict
    cluster_untyped: dict


def _vmid_or_zero(resource: dict[str, object]) -> int:
    try:
        return int(resource.get("vmid", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _prepared_vm_name_sort_key(prepared: _PreparedVMState) -> tuple[str, int]:
    return prepared.cluster_name.casefold(), _vmid_or_zero(prepared.resource)


def _select_prepared_existing_vm(
    prepared: _PreparedVMState,
    cluster_id: int | None,
    vmid: int,
    indexes: _VMNameIndexes,
) -> dict[str, object] | None:
    return _select_existing_vm_record(
        prepared=prepared,
        endpoint_id=extract_proxmox_endpoint_id(prepared.sync_state_fields),
        cluster_id=cluster_id,
        proxmox_vmid=vmid or None,
        endpoint_typed_index=indexes.endpoint_typed,
        endpoint_untyped_candidates=indexes.endpoint_untyped,
        cluster_typed_index=indexes.cluster_typed,
        cluster_untyped_candidates=indexes.cluster_untyped,
    )


def _vm_used_names_by_cluster(
    netbox_snapshot: list[dict[str, object]],
) -> dict[int, set[str]]:
    used_names: dict[int, set[str]] = {}
    for record in netbox_snapshot:
        cluster_id = _relation_id(record.get("cluster"))
        name = record.get("name")
        if cluster_id is not None and isinstance(name, str) and name:
            used_names.setdefault(cluster_id, set()).add(name)
    return used_names


def _group_prepared_vms_by_cluster(
    prepared_vms: list[_PreparedVMState],
) -> dict[int | None, list[_PreparedVMState]]:
    grouped: dict[int | None, list[_PreparedVMState]] = {}
    for prepared in prepared_vms:
        cluster_id = _relation_id(prepared.desired_payload.get("cluster"))
        grouped.setdefault(cluster_id, []).append(prepared)
    return grouped


def _release_prepared_vm_names(
    group: list[_PreparedVMState],
    cluster_id: int | None,
    used_names: set[str],
    indexes: _VMNameIndexes,
) -> None:
    for prepared in group:
        existing = _select_prepared_existing_vm(
            prepared,
            cluster_id,
            _vmid_or_zero(prepared.resource),
            indexes,
        )
        if existing is None:
            continue
        existing_name = existing.get("name")
        if isinstance(existing_name, str) and existing_name in used_names:
            used_names.discard(existing_name)


async def _resolve_prepared_vm_name(
    prepared: _PreparedVMState,
    cluster_id: int | None,
    used_names: set[str],
    indexes: _VMNameIndexes,
    last_synced_names: dict[int, str],
) -> NameResolution | None:
    vmid = _vmid_or_zero(prepared.resource)
    candidate = str(prepared.desired_payload.get("name") or "")
    if not candidate or vmid == 0:
        logger.warning(
            "Skipping VM name pre-pass for vmid=%s: empty name (name=%r). "
            "The VM record may be created/left without a resolved name.",
            vmid or "?",
            candidate,
        )
        return None
    existing = _select_prepared_existing_vm(prepared, cluster_id, vmid, indexes)
    return await resolve_unique_vm_name(
        None,
        netbox_cluster_id=cluster_id,
        proxmox_cluster_name=prepared.cluster_name,
        candidate=candidate,
        proxmox_vmid=vmid,
        used_names_in_cluster=used_names,
        existing_vm_by_vmid={vmid: existing} if existing is not None else {},
        last_synced_proxmox_name=(
            last_synced_names.get(_record_id(existing)) if existing is not None else None
        ),
    )


async def _apply_vm_name_resolution(
    prepared: _PreparedVMState,
    resolution: NameResolution,
    bridge: WebSocketSSEBridge | None,
) -> bool:
    if resolution.operator_renamed or resolution.is_collision:
        prepared.desired_payload["name"] = resolution.resolved_name
    if not resolution.is_collision and not resolution.operator_renamed:
        return False
    vmid = _vmid_or_zero(prepared.resource)
    logger.info(
        "name_collision: cluster=%s vmid=%s %r -> %r (suffix=%s, operator=%s)",
        prepared.cluster_name,
        vmid,
        resolution.original_name,
        resolution.resolved_name,
        resolution.suffix_index,
        resolution.operator_renamed,
    )
    if bridge is not None:
        await bridge.emit_duplicate_name_resolved(
            cluster=prepared.cluster_name,
            original_name=resolution.original_name,
            resolved_name=resolution.resolved_name,
            vmid=vmid,
            suffix_index=resolution.suffix_index,
            operator_renamed=resolution.operator_renamed,
        )
    return True


async def _resolve_vm_names_pre_pass(
    prepared_vms: list[_PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
    bridge: WebSocketSSEBridge | None,
    nb: object | None = None,
) -> list[NameResolution]:
    """Apply deterministic name-collision resolution to ``prepared_vms``.

    Mutates ``prepared.desired_payload["name"]`` in place for any VM that
    collides with another VM in the same NetBox cluster or whose NetBox record
    has been renamed by an operator. Emits ``duplicate_name_resolved`` SSE
    frames via ``bridge`` for each renamed VM.

    Determinism rule: within one NetBox cluster, VMs are processed sorted by
    ``(proxmox_cluster_name.casefold(), proxmox_vmid)``. The first VM keeps
    the bare name; subsequent VMs receive ``" (2)"``, ``" (3)"``, ...
    """

    indexes = _VMNameIndexes(*_build_vm_snapshot_identity_indexes(netbox_snapshot))
    last_synced_names = await load_vm_last_synced_names(nb) if nb is not None else {}
    cluster_used_names = _vm_used_names_by_cluster(netbox_snapshot)
    no_cluster_used_names: set[str] = set()
    resolutions: list[NameResolution] = []
    for cluster_id, group in _group_prepared_vms_by_cluster(prepared_vms).items():
        used_names = (
            no_cluster_used_names
            if cluster_id is None
            else cluster_used_names.setdefault(cluster_id, set())
        )
        _release_prepared_vm_names(group, cluster_id, used_names, indexes)
        for prepared in sorted(group, key=_prepared_vm_name_sort_key):
            resolution = await _resolve_prepared_vm_name(
                prepared,
                cluster_id,
                used_names,
                indexes,
                last_synced_names,
            )
            if resolution is not None and await _apply_vm_name_resolution(
                prepared, resolution, bridge
            ):
                resolutions.append(resolution)
    return resolutions


def _count_vm_operation_methods(
    operation_queue: list[_NetBoxVMOperation],
) -> dict[str, int]:
    """Count queued VM operations by method for reconciliation telemetry."""

    operation_counts: dict[str, int] = {"GET": 0, "CREATE": 0, "UPDATE": 0}
    for operation in operation_queue:
        operation_counts[operation.method] = operation_counts.get(operation.method, 0) + 1
    return operation_counts


def _count_prepared_vm_types(prepared_vms: list[_PreparedVMState]) -> dict[str, int]:
    """Count prepared VM types for reconciliation telemetry."""

    type_counts: dict[str, int] = {"qemu": 0, "lxc": 0, "unknown": 0}
    for prepared in prepared_vms:
        vm_type = str(prepared.vm_type or "unknown").lower()
        if vm_type not in {"qemu", "lxc"}:
            vm_type = "unknown"
        type_counts[vm_type] = type_counts.get(vm_type, 0) + 1
    return type_counts


def _log_vm_reconciliation_measurement(
    *,
    operation_queue: list[_NetBoxVMOperation],
    prepared_vms: list[_PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
    duration_ms: float,
    supports_virtual_machine_type_field: bool,
) -> dict[str, int]:
    """Log the queue-build measurement used by the Rust migration gate."""

    operation_counts = _count_vm_operation_methods(operation_queue)
    type_counts = _count_prepared_vm_types(prepared_vms)
    logger.info(
        "VM reconciliation queue prepared: reconciliation_ms=%.2f vm_count=%d "
        "snapshot_count=%d qemu_count=%d lxc_count=%d unknown_type_count=%d "
        "supports_virtual_machine_type_field=%s GET=%d CREATE=%d UPDATE=%d",
        duration_ms,
        len(prepared_vms),
        len(netbox_snapshot),
        type_counts["qemu"],
        type_counts["lxc"],
        type_counts["unknown"],
        supports_virtual_machine_type_field,
        operation_counts["GET"],
        operation_counts["CREATE"],
        operation_counts["UPDATE"],
    )
    return operation_counts


async def _patch_vm_with_disk_aggregate_retry(
    nb: object,
    *,
    record_id: int,
    payload: dict[str, object],
    cluster_name: str,
    vmid: int,
) -> dict[str, object] | object:
    """Patch a VM, retrying once when NetBox requires the current child-disk sum."""

    try:
        return await rest_patch_async(
            nb,
            "/api/virtualization/virtual-machines/",
            record_id,
            payload,
        )
    except ProxboxException as exc:
        aggregate_disk = extract_vm_disk_aggregate_size(exc)
        if aggregate_disk is None:
            raise

        retry_payload = {**payload, "disk": aggregate_disk}
        logger.warning(
            "Retrying VM patch with NetBox virtual-disk aggregate: cluster=%s vmid=%s "
            "record_id=%s requested_disk=%s aggregate_disk=%s",
            cluster_name,
            vmid,
            record_id,
            payload.get("disk"),
            aggregate_disk,
        )
        return await rest_patch_async(
            nb,
            "/api/virtualization/virtual-machines/",
            record_id,
            retry_payload,
        )


async def _clear_reappeared_vm_marker(
    nb: object,
    vm_record: object,
    vmid: int,
    desired_status: object,
) -> bool:
    """Drop the orphan soft-delete marker from a VM the bulk path just re-adopted.

    The reconciliation queue preserves NetBox tags, so without this a VM that was
    soft-deleted and then reappeared in Proxmox would keep the marker and be excluded
    from every later stage. The reconciliation diff also reads a ``decommissioning``
    status back as ``active`` and never patches it, so the status is restored here from
    the desired Proxmox-derived status. Returns False when the marker could not be cleared.
    """
    try:
        await clear_soft_delete_marker(
            nb,
            vm_record,
            restored_status=ProxmoxToNetBoxVMStatus.from_proxmox(desired_status).value,
        )
    except Exception as exc:
        logger.error("Failed to clear the soft-delete marker for vmid=%s: %s", vmid, exc)
        return False
    return True


async def _dispatch_vm_operation_queue(
    nb: object,
    operation_queue: list[_NetBoxVMOperation],
    *,
    overwrite_vm_role: bool = True,
    overwrite_vm_custom_fields: bool = True,
) -> tuple[dict[tuple[str, int, str], dict[str, object]], set[tuple[str, int, str]]]:
    """Dispatch queued VM operations concurrently, bounded by the write semaphore.

    Operations run concurrently up to ``resolve_netbox_write_concurrency()``
    (default 8, env ``PROXBOX_NETBOX_WRITE_CONCURRENCY``). Per-VM failures are
    isolated: an error on one operation is logged and that VM's key is recorded
    in the returned ``failed_keys`` set instead of raising and aborting the
    whole queue. The caller increments its failed-VM count from ``failed_keys``
    so the failure accounting stays accurate.
    """

    resolved_records: dict[tuple[str, int, str], dict[str, object]] = {}
    failed_keys: set[tuple[str, int, str]] = set()
    if not operation_queue:
        return resolved_records, failed_keys

    write_semaphore = asyncio.Semaphore(max(1, resolve_netbox_write_concurrency()))
    role_snapshot_scan = await scan_vm_last_synced_role_ids(nb)

    def _snapshot_read_for(record: dict[str, object]):
        return resolve_snapshot_read_from_scan(
            role_snapshot_scan,
            record,
        )

    def _role_decision(
        operation: _NetBoxVMOperation,
        existing_record: dict[str, object] | None,
    ) -> RoleSnapshotDecision:
        operation.role_previous_id = (
            _relation_id(existing_record.get("role")) if existing_record is not None else None
        )
        snapshot_read = _snapshot_read_for(existing_record) if existing_record is not None else None
        if existing_record is not None and snapshot_read is not None and not snapshot_read.verified:
            decision = RoleSnapshotDecision(
                role_value=_relation_id(existing_record.get("role")),
                snapshot_value=snapshot_read.snapshot_id,
                write_role=False,
                write_snapshot=False,
            )
        else:
            decision = compute_role_snapshot_decision(
                existing_role_id=(
                    _relation_id(existing_record.get("role"))
                    if existing_record is not None
                    else None
                ),
                existing_snapshot_id=(
                    snapshot_read.snapshot_id if snapshot_read is not None else None
                ),
                desired_role_id=_relation_id(operation.prepared.desired_payload.get("role")),
                overwrite_vm_role=operation.reconciliation_flags.get(
                    "overwrite_vm_role", overwrite_vm_role
                ),
            )
        operation.role_snapshot_id_to_write = (
            decision.snapshot_value if decision.write_snapshot else None
        )
        operation.role_snapshot_previous_id = (
            snapshot_read.snapshot_id if snapshot_read is not None else None
        )
        operation.role_write_applied = decision.write_role
        return decision

    async def _run_single(operation: _NetBoxVMOperation) -> None:
        vmid = int(operation.prepared.resource.get("vmid", 0) or 0)
        key = _prepared_vm_result_key(operation.prepared)
        async with write_semaphore:
            try:
                if operation.method == "GET":
                    if operation.existing_record is None:
                        raise ProxboxException(
                            message="Cannot resolve VM GET without existing NetBox record",
                            python_exception=(
                                f"cluster={operation.prepared.cluster_name} vmid={vmid}"
                            ),
                        )
                    decision = _role_decision(operation, operation.existing_record)
                    if not decision.write_role:
                        resolved_records[key] = operation.existing_record
                        return
                    record_id = _relation_id(operation.existing_record.get("id"))
                    if record_id is None:
                        raise ProxboxException(
                            message="Cannot update VM role without NetBox id",
                            python_exception=(
                                f"cluster={operation.prepared.cluster_name} vmid={vmid}"
                            ),
                        )
                    patched = await _patch_vm_with_disk_aggregate_retry(
                        nb,
                        record_id=record_id,
                        payload={"role": decision.role_value},
                        cluster_name=operation.prepared.cluster_name,
                        vmid=vmid,
                    )
                    resolved_records[key] = _to_mapping(patched) or {
                        **operation.existing_record,
                        "id": record_id,
                        "role": decision.role_value,
                    }
                    return

                if operation.method == "CREATE":
                    existing_resolution = await resolve_virtual_machine_by_sync_state(
                        nb,
                        proxmox_vm_id=vmid,
                        endpoint_id=extract_proxmox_endpoint_id(
                            operation.prepared.sync_state_fields
                        ),
                        cluster_id=_relation_id(operation.prepared.desired_payload.get("cluster")),
                        fail_on_ambiguous=True,
                    )
                    if existing_resolution is not None:
                        existing_record = _to_mapping(existing_resolution.record)
                        recovery_flags = {
                            "overwrite_vm_role": operation.reconciliation_flags.get(
                                "overwrite_vm_role", overwrite_vm_role
                            ),
                            "overwrite_vm_type": operation.reconciliation_flags.get(
                                "overwrite_vm_type", True
                            ),
                            "overwrite_vm_tags": operation.reconciliation_flags.get(
                                "overwrite_vm_tags", True
                            ),
                            "overwrite_vm_description": operation.reconciliation_flags.get(
                                "overwrite_vm_description", True
                            ),
                            "overwrite_vm_custom_fields": operation.reconciliation_flags.get(
                                "overwrite_vm_custom_fields", overwrite_vm_custom_fields
                            ),
                            "supports_virtual_machine_type_field": operation.reconciliation_flags.get(
                                "supports_virtual_machine_type_field", True
                            ),
                        }
                        recovery_operation = _build_vm_operation_queue_python(
                            [operation.prepared],
                            [
                                {
                                    **existing_record,
                                    **operation.prepared.sync_state_fields,
                                }
                            ],
                            **recovery_flags,
                        )
                        recovery_patch_payload = dict(recovery_operation[0].patch_payload)
                        decision = _role_decision(operation, existing_record)
                        if decision.write_role:
                            recovery_patch_payload["role"] = decision.role_value
                        else:
                            recovery_patch_payload.pop("role", None)
                        if not recovery_patch_payload:
                            resolved_records[key] = existing_record
                            return
                        record_id = _relation_id(existing_record.get("id"))
                        if record_id is None:
                            raise ProxboxException(
                                message="Cannot recover existing VM without NetBox id",
                                python_exception=(
                                    f"cluster={operation.prepared.cluster_name} vmid={vmid}"
                                ),
                            )
                        reconciled = await _patch_vm_with_disk_aggregate_retry(
                            nb,
                            record_id=record_id,
                            payload=recovery_patch_payload,
                            cluster_name=operation.prepared.cluster_name,
                            vmid=vmid,
                        )
                        resolved_records[key] = _to_mapping(reconciled) or {
                            **existing_record,
                            **recovery_patch_payload,
                            "id": record_id,
                        }
                        return
                    decision = _role_decision(operation, None)
                    created = await rest_create_async(
                        nb,
                        "/api/virtualization/virtual-machines/",
                        operation.prepared.desired_payload,
                        lookup=operation.prepared.lookup,
                    )
                    resolved_records[key] = _to_mapping(created)
                    return

                if operation.existing_record is None:
                    raise ProxboxException(
                        message="Cannot update VM without existing NetBox record",
                        python_exception=(f"cluster={operation.prepared.cluster_name} vmid={vmid}"),
                    )

                record_id = _relation_id(operation.existing_record.get("id"))
                if record_id is None:
                    raise ProxboxException(
                        message="Cannot update VM without NetBox id",
                        python_exception=f"cluster={operation.prepared.cluster_name} vmid={vmid}",
                    )

                decision = _role_decision(operation, operation.existing_record)
                operation_patch_payload = dict(operation.patch_payload)
                if decision.write_role:
                    operation_patch_payload["role"] = decision.role_value
                else:
                    operation_patch_payload.pop("role", None)
                patch_payload = operation_patch_payload
                if not patch_payload:
                    resolved_records[key] = dict(operation.existing_record)
                    return

                patched = await _patch_vm_with_disk_aggregate_retry(
                    nb,
                    record_id=record_id,
                    payload=patch_payload,
                    cluster_name=operation.prepared.cluster_name,
                    vmid=vmid,
                )
                if isinstance(patched, dict) and patched:
                    resolved_records[key] = patched
                else:
                    merged = dict(operation.existing_record)
                    merged.update(patch_payload)
                    merged["id"] = record_id
                    resolved_records[key] = merged
            except Exception as operation_error:
                # Isolate the failure to this VM; the rest of the queue proceeds.
                logger.warning(
                    "VM operation failed: cluster=%s vmid=%s method=%s error=%s",
                    operation.prepared.cluster_name,
                    vmid,
                    operation.method,
                    operation_error,
                )
                failed_keys.add(key)
                resolved_records.pop(key, None)

    await asyncio.gather(*[_run_single(op) for op in operation_queue], return_exceptions=True)
    return resolved_records, failed_keys


def _parse_vm_networks(vm_config: dict[str, object]) -> list[dict[str, dict[str, str]]]:
    """Extract and parse exact ``net<N>`` entries from a VM config."""
    return parse_proxmox_net_configs(vm_config)


async def _filter_cluster_resources_by_netbox_vm_ids(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    cluster_resources: list[dict],
    netbox_vm_ids: list[int],
    *,
    pxs: object,
    cluster_status: object,
    mode: SelectionMode = SelectionMode.LENIENT,
) -> list[dict]:
    """Delegate selected-resource filtering to the shared ownership implementation.

    Query-selected lists are staged runs, so ownership that cannot be resolved for
    one VM drops that VM with a warning instead of failing the others. The result
    carries the drops on its ``skipped`` attribute.
    """
    return await _filter_selected_vm_resources(
        netbox_session,
        cluster_resources,
        netbox_vm_ids,
        pxs=pxs,
        cluster_status=cluster_status,
        mode=mode,
    )


_VALID_SYNC_MODES = frozenset({"always", "bootstrap_only", "disabled"})


def _normalize_sync_mode(value: str, param_name: str) -> str:
    """Normalize a sync mode string value.

    Accepted values: "always", "bootstrap_only", "disabled".
    Unknown values fall back to "always" with a warning so a bad query param
    never silently blocks a sync.
    """
    if value in _VALID_SYNC_MODES:
        return value
    logger.warning("Unknown %s value %r — falling back to 'always'", param_name, value)
    return "always"


def _vm_resource_allowed_by_sync_modes(
    resource: dict,
    sync_mode_vm: str,
    sync_mode_vm_template: str,
) -> bool:
    """Return True when *resource* should be included in the current sync pass.

    Template detection: truthy ``resource["template"]`` (handles ``1``, ``"1"``,
    ``True``).  Non-template resources are allowed unless *sync_mode_vm* is
    ``"disabled"``.  Template resources are allowed unless *sync_mode_vm_template*
    is ``"disabled"``.  ``"bootstrap_only"`` is treated as enabled at the
    backend — the plugin already tracks whether a VM was bootstrapped; the
    backend simply passes all matching resources through.
    """
    raw_template = resource.get("template")
    is_template = bool(raw_template) and str(raw_template) not in ("0", "false", "False")
    if is_template:
        return sync_mode_vm_template != "disabled"
    return sync_mode_vm != "disabled"


def _filter_cluster_resources_by_sync_modes(
    cluster_resources: list,
    sync_mode_vm: str,
    sync_mode_vm_template: str,
) -> list:
    """Drop VM/template resources disabled by sync mode at the source.

    Applying the filter before discovery and dependency precompute ensures a
    ``disabled`` mode does not create or update dependent NetBox objects
    (manufacturer, device type, cluster, site, node devices, VM roles) for VMs
    that will never be synced. When neither mode is ``disabled`` the input is
    returned unchanged. Non-VM entries and non-dict structures pass through
    untouched so other resource kinds are never affected.
    """
    if sync_mode_vm != "disabled" and sync_mode_vm_template != "disabled":
        return cluster_resources

    filtered: list = []
    skipped = 0
    for cluster in cluster_resources:
        if not isinstance(cluster, dict):
            filtered.append(cluster)
            continue
        new_cluster: dict = {}
        for cluster_name, resources in cluster.items():
            if not isinstance(resources, list):
                new_cluster[cluster_name] = resources
                continue
            kept = []
            for resource in resources:
                if (
                    isinstance(resource, dict)
                    and resource.get("type") in ("qemu", "lxc")
                    and not _vm_resource_allowed_by_sync_modes(
                        resource, sync_mode_vm, sync_mode_vm_template
                    )
                ):
                    skipped += 1
                    continue
                kept.append(resource)
            new_cluster[cluster_name] = kept
        filtered.append(new_cluster)

    if skipped:
        logger.info(
            "VM sync: filtered %d VM resource(s) at source by sync_mode_vm=%r "
            "sync_mode_vm_template=%r",
            skipped,
            sync_mode_vm,
            sync_mode_vm_template,
        )
    return filtered


def _vm_resource_identity(cluster_name: object, resource: object) -> tuple[str, str, int] | None:
    """Return the stable identity of a VM-like cluster resource."""
    if not isinstance(resource, dict):
        return None
    resource_type = resource.get("type")
    vmid = resource.get("vmid")
    if resource_type not in {"qemu", "lxc"} or isinstance(vmid, bool):
        return None
    if isinstance(vmid, int):
        canonical_vmid = vmid
    elif isinstance(vmid, str) and vmid.strip().isascii() and vmid.strip().isdigit():
        canonical_vmid = int(vmid.strip())
    else:
        return None
    if canonical_vmid <= 0:
        return None
    return (str(cluster_name), str(resource_type), canonical_vmid)


def _deduplicate_cluster_vm_resources(
    cluster_name: object,
    resources: list,
    seen: set[tuple[int | None, str, str, int]],
    endpoint_id: int | None,
) -> list:
    """Preserve the first occurrence of each VM identity in one resource list."""
    unique_resources = []
    for resource in resources:
        resource_identity = _vm_resource_identity(cluster_name, resource)
        identity = (endpoint_id, *resource_identity) if resource_identity is not None else None
        if identity is None:
            unique_resources.append(resource)
        elif identity not in seen:
            seen.add(identity)
            unique_resources.append(resource)
    return unique_resources


def _deduplicate_vm_resources_by_identity(
    cluster_resources: list,
    endpoint_ids: list[int | None] | None = None,
) -> list:
    """Keep one VM resource for each cluster, guest type, and VMID.

    Some SDK discovery paths can return the same cluster resource through both
    cluster and node views. Dispatching both copies concurrently creates two
    NetBox rows before either create becomes visible to the other lookup.
    Preserve non-VM resources and the first authoritative VM payload while
    removing only exact guest identities within the same cluster.
    """
    seen: set[tuple[int | None, str, str, int]] = set()
    deduplicated: list = []
    for cluster_index, cluster in enumerate(cluster_resources):
        endpoint_id = endpoint_ids[cluster_index] if endpoint_ids else None
        if not isinstance(cluster, dict):
            deduplicated.append(cluster)
            continue
        normalized_cluster: dict = {}
        for cluster_name, resources in cluster.items():
            if not isinstance(resources, list):
                normalized_cluster[cluster_name] = resources
                continue
            normalized_cluster[cluster_name] = _deduplicate_cluster_vm_resources(
                cluster_name, resources, seen, endpoint_id
            )
        deduplicated.append(normalized_cluster)
    return deduplicated


async def _resolve_netbox_virtual_machine_by_proxmox_id(
    netbox_session: NetBoxSessionDep,
    proxmox_vm_id: int | str | None,
    *,
    endpoint_id: int | None = None,
    cluster_id: int | None = None,
    cluster_name: str | None = None,
) -> dict[str, object] | None:
    """Resolve the NetBox VM row for a Proxmox VM ID, scoped by endpoint when possible."""
    if proxmox_vm_id is None:
        return None

    try:
        vmid = int(str(proxmox_vm_id).strip())
    except (TypeError, ValueError):
        return None

    resolved_endpoint_id = endpoint_id
    resolved_cluster_id = cluster_id
    if resolved_endpoint_id is None and resolved_cluster_id is None and cluster_name:
        resolved_cluster_id = await resolve_netbox_cluster_id_by_name(netbox_session, cluster_name)

    if resolved_endpoint_id is None and resolved_cluster_id is None:
        logger.warning(
            "Resolving NetBox VM by Proxmox VMID %s without endpoint or cluster scope; "
            "caller did not provide a resolvable endpoint or cluster",
            proxmox_vm_id,
        )

    try:
        resolution = await resolve_virtual_machine_by_sync_state(
            netbox_session,
            proxmox_vm_id=vmid,
            endpoint_id=resolved_endpoint_id,
            cluster_id=resolved_cluster_id,
        )
    except Exception as exc:
        error_detail = getattr(exc, "detail", str(exc))
        error_msg = f"{type(exc).__name__}: {error_detail}"
        logger.warning(
            "Could not resolve NetBox VM for Proxmox VMID %s endpoint_id=%s cluster_id=%s: %s",
            proxmox_vm_id,
            resolved_endpoint_id,
            resolved_cluster_id,
            error_msg,
        )
        return None

    if resolution is None:
        return None

    virtual_machine = resolution.record
    if isinstance(virtual_machine, dict):
        return virtual_machine
    if hasattr(virtual_machine, "dict"):
        dumped = virtual_machine.dict()
        if isinstance(dumped, dict):
            return dumped
    return None


async def _create_vm_interface_parallel(
    nb,
    virtual_machine: dict,
    interface_name: str,
    interface_config: dict,
    guest_iface: dict | None,
    tag_refs: list[dict],
    use_guest_agent_interface_name: bool,
    ignore_ipv6_link_local_addresses: bool,
    now: datetime,
    primary_ip_preference: str = "ipv4",
    device: dict | None = None,
    overwrite_flags: SyncOverwriteFlags | None = None,
    dns_name: str | None = None,
    create_ip: bool = True,
    sync_mac: bool = True,
    vm_interface_sync_strategy: object = "guest_os_model",
) -> dict:
    """Create a single VM interface with bridge, VLAN, and IP in parallel-friendly manner.

    Returns a dict with 'interface' (the created interface), 'ip' (the created IP or None),
    and 'first_ip_id' (first IP id found, for setting VM primary_ip).
    """
    from proxbox_api.services.sync.bridge_interfaces import ensure_bridge_interfaces

    vm_id = virtual_machine.get("id")
    result: dict = {"interface": None, "ip": None, "first_ip_id": None}

    bridge_id: int | None = None
    bridge_name = interface_config.get("bridge")
    if bridge_name and vm_id:
        device_id = device.get("id") if isinstance(device, dict) else getattr(device, "id", None)
        bridge_id = await ensure_bridge_interfaces(
            nb,
            device_id,
            vm_id,
            bridge_name,
            tag_refs,
            now,
            overwrite_flags=overwrite_flags,
        )

    vlan_nb_id: int | None = None
    vlan_tag_raw = interface_config.get("tag")
    if vlan_tag_raw is not None:
        try:
            vlan_tag = int(vlan_tag_raw)
            vlan_record = await rest_reconcile_async(
                nb,
                "/api/ipam/vlans/",
                lookup={"vid": vlan_tag},
                payload={
                    "vid": vlan_tag,
                    "name": f"VLAN {vlan_tag}",
                    "status": "active",
                    "tags": tag_refs,
                },
                schema=NetBoxVlanSyncState,
                current_normalizer=lambda record: {
                    "vid": record.get("vid"),
                    "name": record.get("name"),
                    "status": record.get("status"),
                    "tags": record.get("tags"),
                },
            )
            vlan_nb_id = (
                vlan_record.get("id")
                if isinstance(vlan_record, dict)
                else getattr(vlan_record, "id", None)
            )
        except Exception as vlan_exc:
            logger.warning(
                "Failed to create/sync VLAN tag=%s for interface %s: %s",
                vlan_tag_raw,
                interface_name,
                vlan_exc,
            )

    resolved_name, mac_address = _resolve_vm_interface_identity(
        interface_name,
        interface_config,
        guest_iface,
        use_guest_agent_interface_name,
        vm_interface_sync_strategy,
    )
    resolved_name = normalize_vm_interface_name(
        resolved_name,
        fallback=interface_name,
        vm_name=str(virtual_machine.get("name") or ""),
    )

    payload: dict = {
        "name": resolved_name,
        "enabled": True,
        "bridge": None,
        "untagged_vlan": vlan_nb_id,
        "mode": "access" if vlan_nb_id is not None else None,
        "tags": tag_refs,
    }
    if vm_id:
        payload["virtual_machine"] = vm_id

    lookup: dict = {"name": resolved_name}
    if vm_id:
        lookup["virtual_machine_id"] = vm_id

    vm_interface = await rest_reconcile_async(
        nb,
        "/api/virtualization/interfaces/",
        lookup=lookup,
        payload=payload,
        schema=NetBoxVirtualMachineInterfaceSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "virtual_machine": record.get("virtual_machine"),
            "enabled": record.get("enabled"),
            "type": record.get("type"),
            "description": record.get("description"),
            "bridge": record.get("bridge"),
            "untagged_vlan": record.get("untagged_vlan"),
            "mode": record.get("mode"),
            "tags": record.get("tags"),
        },
        nullable_fields={"bridge"},
        strict_lookup=True,
    )
    if not isinstance(vm_interface, dict):
        vm_interface = getattr(vm_interface, "dict", lambda: {})()

    interface_id = (
        vm_interface.get("id")
        if isinstance(vm_interface, dict)
        else getattr(vm_interface, "id", None)
    )
    result["interface"] = vm_interface
    result["interface_id"] = interface_id
    result["mac_address"] = mac_address
    result["interface_name"] = resolved_name
    await write_vm_interface_sync_state(
        nb,
        vm_interface_id=interface_id,
        proxbox_bridge_id=bridge_id,
        overwrite_custom_fields=(
            overwrite_flags is None or overwrite_flags.overwrite_vm_interface_custom_fields
        ),
    )

    if interface_id is not None and mac_address and sync_mac:
        from proxbox_api.services.sync.mac_address import (
            reconcile_mac_for_vm_interface,
        )

        try:
            await reconcile_mac_for_vm_interface(
                nb,
                vminterface_id=int(interface_id),
                mac=mac_address,
                tag_refs=tag_refs,
            )
        except Exception as mac_exc:
            logger.warning(
                "Failed to reconcile MAC %s for VM interface %s: %s",
                mac_address,
                interface_id,
                mac_exc,
            )

    from proxbox_api.services.sync.network import _resolve_vm_interface_ips

    ip_results = await _resolve_vm_interface_ips(
        nb,
        interface_config,
        guest_iface,
        tag_refs,
        interface_id=interface_id,
        interface_name=interface_name,
        now=now,
        create_ip=create_ip,
        ignore_ipv6_link_local=ignore_ipv6_link_local_addresses,
        primary_ip_preference=primary_ip_preference,
        dns_name=dns_name,
    )
    if ip_results:
        first_ip_id, first_ip = ip_results[0]
        result["ip"] = {"id": first_ip_id, "address": first_ip}
        result["first_ip_id"] = first_ip_id
        result["all_ips"] = [{"id": iid, "address": addr} for iid, addr in ip_results]

    return result


async def _create_vm_disk_parallel(
    nb,
    virtual_machine: dict,
    disk_entry,
    cluster_name: str,
    storage_index: dict,
    tag_refs: list[dict],
    now: datetime,
) -> dict | None:
    """Create a single VM disk.

    Returns the created disk record or None on failure.
    """
    storage_name = disk_entry.storage_name or storage_name_from_volume_id(disk_entry.storage)
    storage_record = find_storage_record(
        storage_index,
        cluster_name=cluster_name,
        storage_name=storage_name,
    )
    storage_id = storage_record.get("id") if storage_record else None
    try:
        disk = await rest_reconcile_async(
            nb,
            "/api/virtualization/virtual-disks/",
            lookup={
                "virtual_machine_id": virtual_machine.get("id"),
                "name": disk_entry.name,
            },
            payload={
                "virtual_machine": virtual_machine.get("id"),
                "name": disk_entry.name,
                "size": disk_entry.size_mb,
                "storage": storage_id,
                "description": disk_entry.description,
                "tags": tag_refs,
            },
            schema=NetBoxVirtualDiskSyncState,
            current_normalizer=lambda record: {
                "virtual_machine": record.get("virtual_machine"),
                "name": record.get("name"),
                "size": record.get("size") if record.get("size") is not None else 0,
                "storage": record.get("storage"),
                "description": record.get("description"),
                "tags": record.get("tags"),
            },
            strict_lookup=True,
            nullable_fields={"storage"},
        )
        disk_id = disk.get("id") if isinstance(disk, dict) else getattr(disk, "id", None)
        await write_virtual_disk_sync_state(
            nb,
            virtual_disk_id=disk_id,
            proxbox_storage_id=storage_id,
            overwrite_custom_fields=True,
        )
        return disk
    except Exception as exc:
        error_detail = getattr(exc, "detail", str(exc))
        error_msg = f"{type(exc).__name__}: {error_detail}"
        logger.warning(
            "Failed to create disk %s for VM %s: %s",
            disk_entry.name,
            virtual_machine.get("name"),
            error_msg,
        )
        return None


async def _create_virtual_machine_by_netbox_id(
    *,
    netbox_vm_id: int,
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_websocket: bool = False,
    sync_task_history: bool = True,
    use_guest_agent_interface_name: bool = True,
    vm_interface_sync_strategy: VMInterfaceSyncStrategy = "guest_os_model",
    ignore_ipv6_link_local_addresses: bool = True,
    primary_ip_preference: Literal["ipv4", "ipv6"] = "ipv4",
    overwrite_vm_role: bool | None = None,
    overwrite_vm_type: bool | None = None,
    overwrite_vm_tags: bool | None = None,
    overwrite_vm_description: bool | None = None,
    overwrite_vm_custom_fields: bool | None = None,
    overwrite_flags: SyncOverwriteFlags | None = None,
    run_id: str | None = None,
):
    """Create a single virtual machine by its NetBox ID.

    Looks up the NetBox VM record, extracts metadata, filters Proxmox resources
    for matching VM, and creates/updates the VM in NetBox.
    The delegated VM bundle also reconciles interfaces, IP addresses, disks,
    and task history for the targeted VM.

    Args:
        netbox_vm_id: NetBox virtual machine ID to sync.
        netbox_session: NetBox API session.
        pxs: Proxmox session(s).
        cluster_status: Cluster status objects.
        cluster_resources: Proxmox cluster resources.
        tag: ProxBox tag reference.
        websocket: Optional WebSocket for progress updates.
        use_websocket: Whether to send WebSocket updates.
        use_guest_agent_interface_name: Use guest-agent interface names if available.
        ignore_ipv6_link_local_addresses: Ignore IPv6 link-local addresses when selecting IPs.
        primary_ip_preference: Preferred family when selecting VM primary IP.

    Returns:
        List of created/synced VM records from NetBox.

    Raises:
        HTTPException: If VM not found, missing name, or no matching Proxmox resource.
    """
    vm_record = await netbox_session.virtualization.virtual_machines.get(id=netbox_vm_id)
    if vm_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Virtual machine id={netbox_vm_id} was not found in NetBox.",
        )

    vm_data = _to_mapping(vm_record)
    filtered_for_call = await _filter_selected_vm_resource(
        vm_data,
        cluster_resources,
        netbox_session=netbox_session,
        netbox_vm_id=netbox_vm_id,
        pxs=pxs,
        cluster_status=cluster_status,
    )

    # The strict single-VM filter above already validated this VM. The list-route
    # entry point below is lenient by default, so a VM it still drops (ownership
    # changed between the two reads) must fail this strict route, not degrade it.
    return require_no_dropped_vms(
        await create_virtual_machines(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            cluster_resources=filtered_for_call,
            tag=tag,
            websocket=websocket,
            use_websocket=use_websocket,
            sync_task_history=sync_task_history,
            netbox_vm_ids=str(netbox_vm_id),
            use_guest_agent_interface_name=use_guest_agent_interface_name,
            vm_interface_sync_strategy=vm_interface_sync_strategy,
            ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
            primary_ip_preference=primary_ip_preference,
            overwrite_vm_role=overwrite_vm_role,
            overwrite_vm_type=overwrite_vm_type,
            overwrite_vm_tags=overwrite_vm_tags,
            overwrite_vm_description=overwrite_vm_description,
            overwrite_vm_custom_fields=overwrite_vm_custom_fields,
            overwrite_flags=overwrite_flags
            if overwrite_flags is not None
            else SyncOverwriteFlags(),
            run_id=run_id,
        )
    )


@router.get("/create-test")
async def create_test():
    """
    name:  DB-MASTER
    status:  active
    cluster:  1
    device:  29
    vcpus:  4
    memory:  4294
    disk:  34359
    tags:  [2]
    role:  786
    """

    virtual_machine = await asyncio.to_thread(
        lambda: VirtualMachine(
            name="DB-MASTER",
            status="active",
            cluster=1,
            device=29,
            vcpus=4,
            memory=4294,
            disk=34359,
            tags=[2],
            role=786,
        )
    )

    return virtual_machine


@dataclass(slots=True)
class _VMCreateContext:
    nb: object
    pxs: list[object]
    cluster_status: list[object]
    filtered_cluster_resources: list
    tag: object
    websocket: object | None
    bridge: WebSocketSSEBridge | None
    use_css: bool
    use_websocket: bool
    sync_task_history: bool
    use_guest_agent_interface_name: bool
    normalized_interface_strategy: VMInterfaceSyncStrategy
    ignore_ipv6_link_local_addresses: bool
    primary_ip_preference: Literal["ipv4", "ipv6"]
    overwrite_vm_role: bool
    overwrite_vm_type: bool
    overwrite_vm_tags: bool
    overwrite_vm_description: bool
    overwrite_vm_custom_fields: bool
    overwrite_flags: SyncOverwriteFlags
    behavior_flags: SyncBehaviorFlags
    effective_vm_overwrite_flags: SyncOverwriteFlags
    assign_vm_interface_ips: bool
    sync_vm_interface_macs: bool
    netbox_version: object
    supports_vm_type: bool
    vm_patchable_fields: frozenset[str]
    effective_run_id: str
    selection_skipped: list[dict[str, object]]
    ordered_endpoint_ids: list[int | None]
    px_list: list[object]
    cluster_status_list: list[object]
    endpoint_id_by_cluster: dict[str, int]
    proxmox_url_by_cluster: dict[object, str]
    px_by_cluster: dict[object, object]
    tag_refs: list[dict[str, object]]
    storage_index: dict[tuple[str, str], dict] = field(default_factory=dict)
    cluster_dependency_cache: dict[object, dict[str, object]] = field(default_factory=dict)
    node_device_cache: dict[tuple[int | None, str, str], object] = field(default_factory=dict)
    vm_role_cache: dict[str, object] = field(default_factory=dict)
    vm_type_cache: dict[str, object] = field(default_factory=dict)
    platform_cache: dict[str, int | None] = field(default_factory=dict)
    tag_color_map_by_cluster: dict[str, dict[str, str]] = field(default_factory=dict)
    tag_color_map_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    default_resolved_vm_names: dict[tuple[str, int, str], str] = field(default_factory=dict)
    network_role_snapshot_scan: object | None = None
    required_suboperation_failures: int = 0


@dataclass(slots=True)
class _VMDependencyCluster:
    endpoint_id: int | None
    cluster_name: str
    vm_resources: list[dict]
    cluster_state: object | None
    px_source: object | None


@dataclass(slots=True)
class _VMTaskDependencies:
    cluster: object
    device: object
    role: object
    vm_type_id: int | None
    site_id: int | None
    cluster_dependencies: dict[str, object]


@dataclass(slots=True)
class _VMNetworkResult:
    first_ipv4_id: int | None = None
    first_ipv6_id: int | None = None
    total_interfaces: int = 0
    failed_interfaces: int = 0


def _tag_refs(tag: object) -> list[dict[str, object]]:
    refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    return [ref for ref in refs if ref.get("name") and ref.get("slug")]


async def _build_vm_create_context(
    *,
    netbox_session: object,
    pxs: list[object],
    cluster_status: list[object],
    cluster_resources: list,
    tag: object,
    websocket: object | None,
    use_css: bool,
    use_websocket: bool,
    sync_task_history: bool,
    use_guest_agent_interface_name: bool,
    vm_interface_sync_strategy: VMInterfaceSyncStrategy,
    netbox_vm_ids: str | None,
    ignore_ipv6_link_local_addresses: bool,
    primary_ip_preference: Literal["ipv4", "ipv6"],
    overwrite_vm_role: bool,
    overwrite_vm_type: bool,
    overwrite_vm_tags: bool,
    overwrite_vm_description: bool,
    overwrite_vm_custom_fields: bool,
    overwrite_flags: SyncOverwriteFlags,
    behavior_flags: SyncBehaviorFlags,
    effective_vm_overwrite_flags: SyncOverwriteFlags,
    run_id: str | None,
    assign_vm_interface_ips: bool,
    sync_vm_interface_macs: bool,
    sync_mode_vm: str,
    sync_mode_vm_template: str,
) -> _VMCreateContext:
    normalized_mode = _normalize_sync_mode(sync_mode_vm, "sync_mode_vm")
    normalized_template_mode = _normalize_sync_mode(sync_mode_vm_template, "sync_mode_vm_template")
    normalized_strategy = normalize_vm_interface_sync_strategy(vm_interface_sync_strategy)
    if normalized_strategy == "legacy_rename":
        warn_legacy_vm_interface_strategy()
    netbox_version = await detect_netbox_version(netbox_session)
    supports_vm_type = supports_virtual_machine_type(netbox_version)
    filtered_resources, selection_skipped = await _select_vm_create_resources(
        netbox_session,
        pxs,
        cluster_status,
        cluster_resources,
        netbox_vm_ids,
        normalized_mode,
        normalized_template_mode,
    )
    ordered_endpoint_ids = [
        _session_endpoint_id_at(pxs, index) for index in range(len(filtered_resources))
    ]
    filtered_resources = _deduplicate_vm_resources_by_identity(
        filtered_resources, ordered_endpoint_ids
    )
    (
        px_list,
        cluster_status_list,
        endpoint_id_by_cluster,
        proxmox_url_by_cluster,
        px_by_cluster,
    ) = _build_vm_endpoint_contexts(pxs, cluster_status)
    return _VMCreateContext(
        nb=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        filtered_cluster_resources=filtered_resources,
        tag=tag,
        websocket=websocket,
        bridge=websocket if isinstance(websocket, WebSocketSSEBridge) else None,
        use_css=use_css,
        use_websocket=use_websocket,
        sync_task_history=sync_task_history,
        use_guest_agent_interface_name=use_guest_agent_interface_name,
        normalized_interface_strategy=normalized_strategy,
        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
        primary_ip_preference=primary_ip_preference,
        overwrite_vm_role=overwrite_vm_role,
        overwrite_vm_type=overwrite_vm_type,
        overwrite_vm_tags=overwrite_vm_tags,
        overwrite_vm_description=overwrite_vm_description,
        overwrite_vm_custom_fields=overwrite_vm_custom_fields,
        overwrite_flags=overwrite_flags,
        behavior_flags=behavior_flags,
        effective_vm_overwrite_flags=effective_vm_overwrite_flags,
        assign_vm_interface_ips=assign_vm_interface_ips,
        sync_vm_interface_macs=sync_vm_interface_macs,
        netbox_version=netbox_version,
        supports_vm_type=supports_vm_type,
        vm_patchable_fields=frozenset(
            _compute_vm_patchable_fields(
                effective_vm_overwrite_flags,
                supports_virtual_machine_type_field=supports_vm_type,
            )
        ),
        effective_run_id=(run_id if isinstance(run_id, str) and run_id else str(uuid.uuid4())),
        selection_skipped=selection_skipped,
        ordered_endpoint_ids=ordered_endpoint_ids,
        px_list=px_list,
        cluster_status_list=cluster_status_list,
        endpoint_id_by_cluster=endpoint_id_by_cluster,
        proxmox_url_by_cluster=proxmox_url_by_cluster,
        px_by_cluster=px_by_cluster,
        tag_refs=_tag_refs(tag),
    )


async def _select_vm_create_resources(
    netbox_session: object,
    pxs: list[object],
    cluster_status: list[object],
    cluster_resources: list,
    netbox_vm_ids: str | None,
    sync_mode_vm: str,
    sync_mode_vm_template: str,
) -> tuple[list, list[dict[str, object]]]:
    try:
        selected_vm_ids = parse_selected_netbox_vm_ids(netbox_vm_ids)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    filtered_resources = cluster_resources
    if selected_vm_ids is not None:
        filtered_resources = await _filter_cluster_resources_by_netbox_vm_ids(
            netbox_session=netbox_session,
            cluster_resources=cluster_resources,
            netbox_vm_ids=selected_vm_ids,
            pxs=pxs,
            cluster_status=cluster_status,
        )
    skipped = selection_skips(filtered_resources)
    return (
        _filter_cluster_resources_by_sync_modes(
            filtered_resources, sync_mode_vm, sync_mode_vm_template
        ),
        skipped,
    )


def _vm_discovery_items(filtered_cluster_resources: list) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for cluster in filtered_cluster_resources:
        if not isinstance(cluster, dict):
            continue
        for cluster_name, resources in cluster.items():
            if not isinstance(resources, list):
                continue
            for resource in resources:
                if not isinstance(resource, dict) or resource.get("type") not in ("qemu", "lxc"):
                    continue
                items.append(
                    {
                        "name": str(resource.get("name") or resource.get("vmid") or "unknown"),
                        "type": str(resource.get("type") or "unknown"),
                        "cluster": str(cluster_name),
                        "node": str(resource.get("node") or ""),
                    }
                )
    return items


async def _emit_vm_discovery(context: _VMCreateContext, sync_vm_network: bool) -> None:
    if context.bridge is None:
        return
    items = _vm_discovery_items(context.filtered_cluster_resources)
    await context.bridge.emit_discovery(
        phase="virtual-machines",
        items=items,
        message=f"Discovered {len(items)} virtual machine(s) to synchronize",
        metadata={"sync_vm_network": sync_vm_network},
    )


async def _get_cluster_tag_color_map(
    context: _VMCreateContext,
    cluster_name: str,
) -> dict[str, str]:
    if cluster_name in context.tag_color_map_by_cluster:
        return context.tag_color_map_by_cluster[cluster_name]
    lock = context.tag_color_map_locks.setdefault(cluster_name, asyncio.Lock())
    async with lock:
        if cluster_name in context.tag_color_map_by_cluster:
            return context.tag_color_map_by_cluster[cluster_name]
        px = context.px_by_cluster.get(cluster_name)
        context.tag_color_map_by_cluster[cluster_name] = (
            await fetch_tag_color_map(px) if px is not None else {}
        )
    return context.tag_color_map_by_cluster[cluster_name]


async def _resolve_vm_proxmox_tag_ids(
    context: _VMCreateContext,
    cluster_name: str,
    vm_config: dict[str, object],
) -> list[int]:
    if not context.overwrite_flags.overwrite_vm_proxmox_tags:
        return []
    raw = vm_config.get("tags") if isinstance(vm_config, dict) else None
    if not raw:
        return []
    color_map = await _get_cluster_tag_color_map(context, cluster_name)
    return await resolve_proxmox_tag_ids(context.nb, raw, color_map=color_map)


async def _sync_scoped_vm_task_history(
    context: _VMCreateContext,
    records: list[object],
) -> dict[str, object] | None:
    if not context.sync_task_history:
        return None
    vm_ids = sorted(
        {
            vm_id
            for record in records
            if (vm_id := _relation_id(_to_mapping(record).get("id"))) is not None
        }
    )
    if not vm_ids:
        return None
    result = await sync_all_virtual_machine_task_histories(
        netbox_session=context.nb,
        pxs=context.pxs,
        cluster_status=context.cluster_status,
        tag_refs=context.tag_refs,
        websocket=context.websocket,
        use_websocket=context.use_websocket,
        netbox_vm_ids=vm_ids,
    )
    _preserve_task_history_degradation(result, use_websocket=context.use_websocket)
    return result


def _collect_vm_dependency_clusters(
    context: _VMCreateContext,
) -> list[_VMDependencyCluster]:
    endpoint_clusters: list[_VMDependencyCluster] = []
    for index, cluster in enumerate(context.filtered_cluster_resources):
        if not isinstance(cluster, dict):
            continue
        cluster_state = (
            context.cluster_status_list[index] if index < len(context.cluster_status_list) else None
        )
        px_source = context.px_list[index] if index < len(context.px_list) else None
        for cluster_name, resources in cluster.items():
            if not isinstance(resources, list):
                continue
            vm_resources = [
                resource
                for resource in resources
                if isinstance(resource, dict) and resource.get("type") in ("qemu", "lxc")
            ]
            if vm_resources:
                endpoint_clusters.append(
                    _VMDependencyCluster(
                        endpoint_id=context.ordered_endpoint_ids[index],
                        cluster_name=str(cluster_name),
                        vm_resources=vm_resources,
                        cluster_state=cluster_state,
                        px_source=px_source,
                    )
                )
    return endpoint_clusters


async def _precompute_single_vm_cluster(
    context: _VMCreateContext,
    dependency_cluster: _VMDependencyCluster,
    device_type: object,
    device_role: object,
) -> set[str]:
    cluster_mode = getattr(dependency_cluster.cluster_state, "mode", None) or "cluster"
    cluster_type, site, tenant = await asyncio.gather(
        _ensure_cluster_type(context.nb, mode=cluster_mode, tag_refs=context.tag_refs),
        _ensure_site(
            context.nb,
            cluster_name=dependency_cluster.cluster_name,
            tag_refs=context.tag_refs,
            placement=dependency_cluster.cluster_state,
        ),
        _resolve_tenant(context.nb, placement=dependency_cluster.cluster_state),
    )
    cluster = await _ensure_cluster(
        context.nb,
        cluster_name=dependency_cluster.cluster_name,
        cluster_type_id=getattr(cluster_type, "id", None),
        mode=cluster_mode,
        tag_refs=context.tag_refs,
        site_id=getattr(site, "id", None),
        tenant_id=getattr(tenant, "id", None),
        overwrite_flags=context.overwrite_flags,
    )
    site_id = _effective_cluster_site_id(cluster, fallback_site_id=getattr(site, "id", None))
    context.cluster_dependency_cache[
        (dependency_cluster.endpoint_id, dependency_cluster.cluster_name)
    ] = {
        "cluster": cluster,
        "site": site,
        "site_id": site_id,
        "tenant": tenant,
        "device_type": device_type,
        "device_role": device_role,
    }
    await _precompute_vm_node_devices(
        context,
        dependency_cluster,
        cluster,
        device_type,
        device_role,
        site_id,
    )
    return {
        vm_type if vm_type in VM_ROLE_MAPPINGS else "undefined"
        for resource in dependency_cluster.vm_resources
        if (vm_type := str(resource.get("type") or "undefined").lower())
    }


async def _precompute_vm_node_devices(
    context: _VMCreateContext,
    dependency_cluster: _VMDependencyCluster,
    cluster: object,
    device_type: object,
    device_role: object,
    site_id: int | None,
) -> None:
    node_names = sorted(
        {
            str(resource.get("node"))
            for resource in dependency_cluster.vm_resources
            if resource.get("node") is not None
        }
    )
    naming_kwargs = _endpoint_node_naming_kwargs(
        dependency_cluster.cluster_state or dependency_cluster.px_source,
        dependency_cluster.cluster_name,
        dependency_cluster.endpoint_id,
    )
    for node_name in node_names:
        context.node_device_cache[
            (dependency_cluster.endpoint_id, dependency_cluster.cluster_name, node_name)
        ] = await _ensure_device(
            context.nb,
            device_name=node_name,
            **naming_kwargs,
            cluster_id=getattr(cluster, "id", None),
            device_type_id=getattr(device_type, "id", None),
            role_id=getattr(device_role, "id", None),
            site_id=site_id,
            tag_refs=context.tag_refs,
            overwrite_device_role=context.overwrite_flags.overwrite_device_role,
            overwrite_device_type=context.overwrite_flags.overwrite_device_type,
            overwrite_device_tags=context.overwrite_flags.overwrite_device_tags,
            overwrite_flags=context.overwrite_flags,
        )


async def _precompute_vm_roles_and_types(
    context: _VMCreateContext,
    vm_types: set[str],
) -> None:
    sorted_vm_types = sorted(vm_types)
    role_results = await asyncio.gather(
        *[
            rest_reconcile_async(
                context.nb,
                "/api/dcim/device-roles/",
                lookup={"slug": VM_ROLE_MAPPINGS[vm_type].get("slug")},
                payload={**VM_ROLE_MAPPINGS[vm_type], "tags": context.tag_refs},
                schema=NetBoxDeviceRoleSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "color": record.get("color"),
                    "description": record.get("description"),
                    "vm_role": record.get("vm_role"),
                    "tags": record.get("tags"),
                },
            )
            for vm_type in sorted_vm_types
        ],
        return_exceptions=True,
    )
    for vm_type, role in zip(sorted_vm_types, role_results):
        if not isinstance(role, BaseException):
            context.vm_role_cache[vm_type] = role
    if context.supports_vm_type:
        await _precompute_vm_types(context, sorted_vm_types)


async def _precompute_vm_types(
    context: _VMCreateContext,
    sorted_vm_types: list[str],
) -> None:
    results = await asyncio.gather(
        *[
            ensure_vm_type(
                context.nb,
                vm_type,
                context.tag_refs,
                netbox_version=context.netbox_version,
            )
            for vm_type in sorted_vm_types
        ],
        return_exceptions=True,
    )
    for vm_type, result in zip(sorted_vm_types, results):
        if result is not None and not isinstance(result, BaseException):
            context.vm_type_cache[vm_type] = result


async def _precompute_vm_dependencies(context: _VMCreateContext) -> None:
    dependency_clusters = _collect_vm_dependency_clusters(context)
    if not dependency_clusters:
        return
    manufacturer = await _ensure_manufacturer(context.nb, tag_refs=context.tag_refs)
    device_type = await _ensure_device_type(
        context.nb,
        manufacturer_id=getattr(manufacturer, "id", None),
        tag_refs=context.tag_refs,
    )
    device_role = await _ensure_proxmox_node_role(context.nb, tag_refs=context.tag_refs)
    cluster_results = await asyncio.gather(
        *[
            _precompute_single_vm_cluster(context, dependency_cluster, device_type, device_role)
            for dependency_cluster in dependency_clusters
        ],
        return_exceptions=True,
    )
    for result in cluster_results:
        if isinstance(result, BaseException):
            raise result
    vm_types = set().union(*cast("list[set[str]]", cluster_results))
    await _precompute_vm_roles_and_types(context, vm_types)


async def _load_vm_create_storage_index(context: _VMCreateContext) -> None:
    try:
        records = await rest_list_async(context.nb, "/api/plugins/proxbox/storage/")
        context.storage_index = build_storage_index(records)
    except Exception as error:
        detail = getattr(error, "detail", str(error))
        logger.warning(
            "Error loading storage records for VM sync: %s: %s",
            type(error).__name__,
            detail,
        )


async def _resolve_cached_vm_type(
    context: _VMCreateContext,
    vm_type_key: str,
) -> object | None:
    if not context.supports_vm_type:
        return None
    if vm_type_key not in context.vm_type_cache and vm_type_key in VM_TYPE_MAPPINGS:
        result = await ensure_vm_type(
            context.nb,
            vm_type_key,
            context.tag_refs,
            netbox_version=context.netbox_version,
        )
        if result is not None:
            context.vm_type_cache[vm_type_key] = result
    return context.vm_type_cache.get(vm_type_key)


def _vm_preparation_context(context: _VMCreateContext) -> _VMPreparationContext:
    return _VMPreparationContext(
        nb=context.nb,
        tag=context.tag,
        overwrite_flags=context.overwrite_flags,
        behavior_flags=context.behavior_flags,
        effective_vm_overwrite_flags=context.effective_vm_overwrite_flags,
        cluster_dependency_cache=context.cluster_dependency_cache,
        node_device_cache=context.node_device_cache,
        vm_role_cache=context.vm_role_cache,
        vm_role_mapping=VM_ROLE_MAPPINGS,
        tag_refs=context.tag_refs,
        proxmox_url_by_cluster=context.proxmox_url_by_cluster,
        endpoint_id_by_cluster=context.endpoint_id_by_cluster,
        px_by_cluster=context.px_by_cluster,
        platform_cache=context.platform_cache,
        resolve_vm_type=lambda key: _resolve_cached_vm_type(context, key),
        resolve_vm_proxmox_tag_ids=lambda cluster_name, config: _resolve_vm_proxmox_tag_ids(
            context, cluster_name, config
        ),
    )


def _collect_full_update_vm_inputs(
    context: _VMCreateContext,
) -> list[tuple[int | None, str, dict, object | None, object | None]]:
    operation_inputs = []
    for index, cluster in enumerate(context.filtered_cluster_resources):
        if not isinstance(cluster, dict):
            continue
        px_source = context.px_list[index] if index < len(context.px_list) else None
        cluster_source = (
            context.cluster_status_list[index] if index < len(context.cluster_status_list) else None
        )
        for cluster_name, resources in cluster.items():
            if not isinstance(resources, list):
                continue
            operation_inputs.extend(
                (
                    context.ordered_endpoint_ids[index],
                    str(cluster_name),
                    resource,
                    px_source,
                    cluster_source,
                )
                for resource in resources
                if isinstance(resource, dict) and resource.get("type") in ("qemu", "lxc")
            )
    return operation_inputs


async def _fetch_one_full_update_vm_config(
    context: _VMCreateContext,
    telemetry: _VMFullUpdateTelemetry,
    timeout_seconds: float,
    operation_input: tuple[int | None, str, dict, object | None, object | None],
) -> dict[str, object]:
    endpoint_id, cluster_name, resource, px_source, _cluster_source = operation_input
    cluster_px = px_source or context.px_by_cluster.get((endpoint_id, cluster_name))
    fetch_pxs = [cluster_px] if cluster_px is not None else context.pxs
    request_started = time.perf_counter()
    try:
        return await asyncio.wait_for(
            _fetch_vm_config_only(pxs=fetch_pxs, resource=resource),
            timeout=timeout_seconds,
        )
    finally:
        telemetry.fetch_latencies_ms.append((time.perf_counter() - request_started) * 1000)


async def _fetch_full_update_vm_configs(
    context: _VMCreateContext,
    telemetry: _VMFullUpdateTelemetry,
    operation_inputs: list[tuple[int | None, str, dict, object | None, object | None]],
) -> tuple[list[tuple], int, float]:
    configured_workers = max(1, resolve_vm_sync_concurrency())
    worker_count, queue_capacity = _bounded_map_capacity(len(operation_inputs), configured_workers)
    telemetry.fetch_workers = worker_count
    telemetry.fetch_queue_capacity = queue_capacity
    timeout_seconds = resolve_vm_config_fetch_timeout_seconds()
    fetch_started = time.perf_counter()
    fetch_results = await _map_bounded_ordered(
        operation_inputs,
        lambda item: _fetch_one_full_update_vm_config(context, telemetry, timeout_seconds, item),
        worker_count=worker_count,
    )
    fetched_configs: list[tuple] = []
    failed_count = 0
    for operation_input, fetch_result in zip(operation_inputs, fetch_results):
        if isinstance(fetch_result, Exception):
            _log_vm_config_fetch_failure(operation_input, fetch_result)
            failed_count += 1
        else:
            fetched_configs.append((*operation_input[:3], fetch_result, *operation_input[3:]))
    return fetched_configs, failed_count, (time.perf_counter() - fetch_started) * 1000


def _log_vm_config_fetch_failure(operation_input: tuple, error: Exception) -> None:
    _endpoint_id, cluster_name, resource, _px_source, _cluster_source = operation_input
    logger.warning(
        "VM config fetch failed: cluster=%s vmid=%s error=%s",
        cluster_name,
        resource.get("vmid"),
        error,
    )


async def _prepare_full_update_vms(
    fetched_configs: list[tuple],
    prepare_context: _VMPreparationContext,
) -> tuple[list[_PreparedVMState], int, float]:
    started = time.perf_counter()
    prepared_vms: list[_PreparedVMState] = []
    failed_count = 0
    for (
        endpoint_id,
        cluster_name,
        resource,
        vm_config,
        px_source,
        cluster_source,
    ) in fetched_configs:
        try:
            prepared_vms.append(
                await _prepare_vm_from_config(
                    cluster_name,
                    resource,
                    vm_config,
                    prepare_context,
                    endpoint_id=endpoint_id,
                    cluster_source=cluster_source,
                    px_source=px_source,
                )
            )
        except Exception as error:
            logger.warning(
                "VM preparation failed: cluster=%s vmid=%s error=%s",
                cluster_name,
                resource.get("vmid"),
                error,
            )
            failed_count += 1
    return prepared_vms, failed_count, (time.perf_counter() - started) * 1000


async def _build_full_update_operation_queue(
    context: _VMCreateContext,
    telemetry: _VMFullUpdateTelemetry,
    prepared_vms: list[_PreparedVMState],
) -> tuple[list[_NetBoxVMOperation], list[dict[str, object]], float]:
    started = time.perf_counter()
    snapshot = await _load_netbox_virtual_machine_snapshot(context.nb, fresh=True)
    telemetry.snapshot_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    await _hydrate_vm_snapshot_with_sidecar_identity(
        context.nb,
        prepared_vms=prepared_vms,
        netbox_snapshot=snapshot,
        configured_endpoint_ids=_ConfiguredEndpointIds(context.nb, context.px_list),
    )
    telemetry.hydration_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    await _resolve_vm_names_pre_pass(prepared_vms, snapshot, context.bridge, context.nb)
    telemetry.name_resolution_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    desired_states = await asyncio.to_thread(_finalize_desired_vm_states, prepared_vms)
    for prepared, desired_state in zip(prepared_vms, desired_states):
        prepared.desired_state = desired_state
    telemetry.canonicalization_ms = (time.perf_counter() - started) * 1000
    reconciliation_started = time.perf_counter()
    context.selection_skipped.extend(unverifiable_vm_warnings(prepared_vms, snapshot))
    queue = _build_vm_operation_queue(
        prepared_vms,
        snapshot,
        overwrite_vm_role=context.overwrite_vm_role,
        overwrite_vm_type=context.overwrite_vm_type,
        overwrite_vm_tags=context.overwrite_vm_tags,
        overwrite_vm_description=context.overwrite_vm_description,
        overwrite_vm_custom_fields=context.overwrite_vm_custom_fields,
        supports_virtual_machine_type_field=context.supports_vm_type,
    )
    reconciliation_ms = (time.perf_counter() - reconciliation_started) * 1000
    _log_vm_reconciliation_measurement(
        operation_queue=queue,
        prepared_vms=prepared_vms,
        netbox_snapshot=snapshot,
        duration_ms=reconciliation_ms,
        supports_virtual_machine_type_field=context.supports_vm_type,
    )
    return queue, snapshot, reconciliation_ms


async def _persist_one_full_update_vm(
    context: _VMCreateContext,
    operation: _NetBoxVMOperation,
    vm_record: dict[str, object],
) -> bool:
    vmid = int(operation.prepared.resource.get("vmid", 0) or 0)
    try:
        await persist_sync_state_with_role_compensation(
            context.nb,
            persistence=write_virtual_machine_sync_state(
                context.nb,
                virtual_machine_id=vm_record.get("id"),
                custom_fields=operation.prepared.sync_state_fields,
                overwrite_custom_fields=context.overwrite_vm_custom_fields,
                proxmox_vm_name=operation.prepared.resource.get("name"),
                proxmox_last_synced_role_id=operation.role_snapshot_id_to_write,
            ),
            virtual_machine_id=_relation_id(vm_record.get("id")),
            previous_role_id=operation.role_previous_id,
            previous_snapshot_id=operation.role_snapshot_previous_id,
            expected_snapshot_id=operation.role_snapshot_id_to_write,
            role_write_applied=operation.role_write_applied,
        )
    except Exception as error:
        logger.error(
            "VM role ownership snapshot failed after reconciliation: cluster=%s vmid=%s error=%s",
            operation.prepared.cluster_name,
            vmid,
            error,
        )
        return False
    await stamp_vm_last_run_id(context.nb, vm_record, context.effective_run_id)
    return await _clear_reappeared_vm_marker(
        context.nb,
        vm_record,
        vmid,
        operation.prepared.desired_payload.get("status"),
    )


async def _persist_full_update_vm_results(
    context: _VMCreateContext,
    operation_queue: list[_NetBoxVMOperation],
    resolved_records: dict[tuple[str, int, str], dict[str, object]],
    failed_operation_keys: set[tuple[str, int, str]],
) -> tuple[list[dict[str, object]], int]:
    results: list[dict[str, object]] = []
    failed_count = 0
    for operation in operation_queue:
        key = _prepared_vm_result_key(operation.prepared)
        if key in failed_operation_keys:
            failed_count += 1
            continue
        vm_record = resolved_records.get(key)
        if vm_record is None and operation.existing_record is not None:
            vm_record = operation.existing_record
        if vm_record is None:
            logger.warning(
                "VM operation completed without resolved NetBox record: "
                "cluster=%s vmid=%s method=%s",
                operation.prepared.cluster_name,
                int(operation.prepared.resource.get("vmid", 0) or 0),
                operation.method,
            )
            failed_count += 1
            continue
        if not await _persist_one_full_update_vm(context, operation, vm_record):
            failed_count += 1
            continue
        results.append(vm_record)
    return results, failed_count


def _log_full_update_batch_completion(
    telemetry: _VMFullUpdateTelemetry,
    *,
    batch_started: float,
    batch_cpu_started: float,
    reconciliation_ms: float,
    fetch_ms: float,
    process_ms: float,
    prepared_count: int,
    snapshot_count: int,
    failed_count: int,
) -> None:
    batch_ms = (time.perf_counter() - batch_started) * 1000
    share = (reconciliation_ms / batch_ms) * 100 if batch_ms > 0 else 0.0
    logger.info(
        "VM full-update batch timing: total_ms=%.2f reconciliation_ms=%.2f "
        "fetch_ms=%.2f process_ms=%.2f reconciliation_share_pct=%.2f "
        "vm_count=%d snapshot_count=%d",
        batch_ms,
        reconciliation_ms,
        fetch_ms,
        process_ms,
        share,
        prepared_count,
        snapshot_count,
    )
    _log_vm_full_update_terminal(
        outcome=_vm_full_update_outcome(
            failed_count=failed_count,
            prepared_count=prepared_count,
        ),
        total_ms=batch_ms,
        process_cpu_ms=(time.process_time() - batch_cpu_started) * 1000,
        snapshot_ms=telemetry.snapshot_ms,
        hydration_ms=telemetry.hydration_ms,
        name_resolution_ms=telemetry.name_resolution_ms,
        canonicalization_ms=telemetry.canonicalization_ms,
        dispatch_ms=telemetry.dispatch_ms,
        persistence_ms=telemetry.persistence_ms,
        fetch_requests=len(telemetry.fetch_latencies_ms),
        fetch_latency_total_ms=sum(telemetry.fetch_latencies_ms),
        fetch_latency_max_ms=max(telemetry.fetch_latencies_ms, default=0.0),
        fetch_workers=telemetry.fetch_workers,
        fetch_queue_capacity=telemetry.fetch_queue_capacity,
        peak_rss_kib=process_resource.getrusage(process_resource.RUSAGE_SELF).ru_maxrss,
    )


def _log_empty_prepared_vm_batch(
    telemetry: _VMFullUpdateTelemetry,
    batch_started: float,
    batch_cpu_started: float,
    failed_count: int,
) -> None:
    _log_vm_full_update_terminal(
        outcome=_vm_full_update_outcome(failed_count=failed_count, prepared_count=0),
        total_ms=(time.perf_counter() - batch_started) * 1000,
        process_cpu_ms=(time.process_time() - batch_cpu_started) * 1000,
        fetch_requests=len(telemetry.fetch_latencies_ms),
        fetch_latency_total_ms=sum(telemetry.fetch_latencies_ms),
        fetch_latency_max_ms=max(telemetry.fetch_latencies_ms, default=0.0),
        fetch_workers=telemetry.fetch_workers,
        fetch_queue_capacity=telemetry.fetch_queue_capacity,
    )


async def _run_full_update_vm_batch(
    context: _VMCreateContext,
    prepare_context: _VMPreparationContext,
    telemetry: _VMFullUpdateTelemetry,
) -> tuple[list[dict[str, object]], int]:
    batch_started = time.perf_counter()
    batch_cpu_started = time.process_time()
    operation_inputs = _collect_full_update_vm_inputs(context)
    if not operation_inputs:
        return [], 0
    fetched_configs, fetch_failed, fetch_ms = await _fetch_full_update_vm_configs(
        context, telemetry, operation_inputs
    )
    prepared_vms, prepare_failed, process_ms = await _prepare_full_update_vms(
        fetched_configs, prepare_context
    )
    failed_count = fetch_failed + prepare_failed
    logger.info(
        "VM full-update phase timing: fetch_ms=%.2f process_ms=%.2f fetched_ok=%d fetch_failed=%d",
        fetch_ms,
        process_ms,
        len(fetched_configs),
        fetch_failed,
    )
    if not prepared_vms:
        _log_empty_prepared_vm_batch(telemetry, batch_started, batch_cpu_started, failed_count)
        return [], failed_count
    operation_queue, snapshot, reconciliation_ms = await _build_full_update_operation_queue(
        context, telemetry, prepared_vms
    )
    started = time.perf_counter()
    resolved_records, failed_keys = await _dispatch_vm_operation_queue(
        context.nb,
        operation_queue,
        overwrite_vm_role=context.overwrite_vm_role,
        overwrite_vm_custom_fields=context.overwrite_vm_custom_fields,
    )
    telemetry.dispatch_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    results, persistence_failures = await _persist_full_update_vm_results(
        context, operation_queue, resolved_records, failed_keys
    )
    failed_count += persistence_failures
    telemetry.persistence_ms = (time.perf_counter() - started) * 1000
    _log_full_update_batch_completion(
        telemetry,
        batch_started=batch_started,
        batch_cpu_started=batch_cpu_started,
        reconciliation_ms=reconciliation_ms,
        fetch_ms=fetch_ms,
        process_ms=process_ms,
        prepared_count=len(prepared_vms),
        snapshot_count=len(snapshot),
        failed_count=failed_count,
    )
    return results, failed_count


async def _emit_vm_sync_summary(
    context: _VMCreateContext,
    successful_count: int,
    failed_count: int,
    *,
    concise_success: bool,
) -> None:
    if context.bridge is not None:
        if concise_success and failed_count == 0:
            message = f"Virtual machine sync completed: {successful_count} synchronized"
        elif concise_success:
            message = (
                "Virtual machine sync completed with errors: "
                f"{successful_count} synchronized, {failed_count} failed"
            )
        else:
            message = (
                f"Virtual machine sync completed: {successful_count} synchronized, "
                f"{failed_count} failed"
            )
        await context.bridge.emit_phase_summary(
            phase="virtual-machines",
            created=successful_count,
            failed=failed_count,
            message=message,
        )
    if context.use_websocket and context.websocket:
        await context.websocket.send_json({"object": "virtual_machine", "end": True})


def _log_vm_sync_summary(total_count: int, successful_count: int, failed_count: int) -> None:
    global_cache.clear_cache()
    logger.info(
        "VM sync summary: total=%s ok=%s failed=%s",
        total_count,
        successful_count,
        failed_count,
    )


async def _run_vm_create_without_network(
    context: _VMCreateContext,
    prepare_context: _VMPreparationContext,
) -> object:
    telemetry = _VMFullUpdateTelemetry()
    results, failed_count = await _run_vm_full_update_with_cancellation_telemetry(
        lambda: _run_full_update_vm_batch(context, prepare_context, telemetry),
        telemetry,
    )
    await _sync_scoped_vm_task_history(context, results)
    successful_count = len(results)
    total_count = successful_count + failed_count
    await _emit_vm_sync_summary(
        context,
        successful_count,
        failed_count,
        concise_success=True,
    )
    _log_vm_sync_summary(total_count, successful_count, failed_count)
    return _vm_stage_result(
        results,
        context.selection_skipped,
        failed_count=failed_count,
    )


def _network_name_prepass_vms(context: _VMCreateContext) -> list[_PreparedVMState]:
    prepared_vms: list[_PreparedVMState] = []
    now = datetime.now(timezone.utc)
    for index, cluster in enumerate(context.filtered_cluster_resources):
        if not isinstance(cluster, dict):
            continue
        endpoint_id = context.ordered_endpoint_ids[index]
        for cluster_name, resources in cluster.items():
            if not isinstance(resources, list):
                continue
            cluster_name_text = str(cluster_name)
            cluster_obj = context.cluster_dependency_cache.get(
                (endpoint_id, cluster_name_text), {}
            ).get("cluster")
            cluster_id = _relation_id(cluster_obj) or _relation_id(getattr(cluster_obj, "id", None))
            for resource in resources:
                prepared = _network_name_prepass_vm(
                    resource,
                    cluster_name_text,
                    cluster_id,
                    endpoint_id,
                    now,
                )
                if prepared is not None:
                    prepared_vms.append(prepared)
    return prepared_vms


def _network_name_prepass_vm(
    resource: object,
    cluster_name: str,
    cluster_id: int | None,
    endpoint_id: int | None,
    now: datetime,
) -> _PreparedVMState | None:
    if not isinstance(resource, dict):
        return None
    vm_type = str(resource.get("type") or "").strip().lower()
    if vm_type not in ("qemu", "lxc"):
        return None
    try:
        vmid = int(resource.get("vmid") or 0)
    except (TypeError, ValueError):
        vmid = 0
    if vmid <= 0:
        return None
    return _PreparedVMState(
        cluster_name=cluster_name,
        resource=resource,
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload={"name": str(resource.get("name") or ""), "cluster": cluster_id},
        lookup=_missing_vm_lookup(),
        now=now,
        vm_type=vm_type,
        sync_state_fields={
            "proxmox_endpoint_id": endpoint_id,
            "proxmox_vm_id": vmid,
            "proxmox_vm_type": vm_type,
        },
    )


async def _resolve_network_vm_names(context: _VMCreateContext) -> None:
    prepared_vms = _network_name_prepass_vms(context)
    if not prepared_vms:
        return
    snapshot = await _load_netbox_virtual_machine_snapshot(context.nb, fresh=True)
    await _hydrate_vm_snapshot_with_sidecar_identity(
        context.nb,
        prepared_vms=prepared_vms,
        netbox_snapshot=snapshot,
        configured_endpoint_ids=_ConfiguredEndpointIds(context.nb, context.px_list),
    )
    await _resolve_vm_names_pre_pass(prepared_vms, snapshot, context.bridge, context.nb)
    context.default_resolved_vm_names = {
        _prepared_vm_result_key(prepared): resolved_name
        for prepared in prepared_vms
        if isinstance((resolved_name := prepared.desired_payload.get("name")), str)
        and resolved_name
    }


async def _network_vm_config(
    context: _VMCreateContext,
    resource: dict,
    px_source: object | None,
) -> tuple[dict, ProxmoxVmConfigInput]:
    result = get_vm_config(
        pxs=[px_source] if px_source is not None else context.pxs,
        node=resource.get("node"),
        type=resource.get("type", "unknown"),
        vmid=resource.get("vmid"),
    )
    if inspect.isawaitable(result):
        result = await result
    config = result or {}
    return config, ProxmoxVmConfigInput.model_validate(config)


def _initial_vm_progress_payload(
    context: _VMCreateContext,
    cluster_name: object,
    resource: dict,
) -> dict:
    undefined_html = return_status_html("undefined", context.use_css)
    return {
        "sync_status": return_status_html("syncing", context.use_css),
        "name": str(resource.get("name")),
        "netbox_id": undefined_html,
        "status": undefined_html,
        "cluster": str(cluster_name),
        "device": str(resource.get("node")),
        "role": undefined_html,
        "vcpus": undefined_html,
        "memory": undefined_html,
        "disk": undefined_html,
        "vm_interfaces": undefined_html,
        "completed": False,
        "rowid": str(resource.get("name")),
    }


async def _emit_vm_task_start(
    context: _VMCreateContext,
    cluster_name: object,
    resource: dict,
    vm_name: str,
    timing_key: str,
) -> None:
    if context.bridge is not None:
        context.bridge.start_timer(timing_key)
        await context.bridge.emit_item_progress(
            phase="virtual-machines",
            item={
                "name": vm_name,
                "type": str(resource.get("type") or "unknown"),
                "cluster": str(cluster_name),
                "node": str(resource.get("node") or ""),
            },
            operation=ItemOperation.CREATED,
            status="processing",
            message=f"Processing VM '{vm_name}'",
            progress_current=0,
            progress_total=0,
        )
    if context.use_websocket and context.websocket:
        await context.websocket.send_json(
            {
                "object": "virtual_machine",
                "type": "create",
                "data": _initial_vm_progress_payload(context, cluster_name, resource),
            }
        )


async def _resolve_vm_task_role(
    context: _VMCreateContext,
    vm_type_key: str,
) -> object:
    role = context.vm_role_cache.get(vm_type_key)
    if role is not None:
        return role
    role_payload = VM_ROLE_MAPPINGS.get(vm_type_key, VM_ROLE_MAPPINGS["undefined"])
    role = await rest_reconcile_async(
        context.nb,
        "/api/dcim/device-roles/",
        lookup={"slug": role_payload.get("slug")},
        payload={**role_payload, "tags": context.tag_refs},
        schema=NetBoxDeviceRoleSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "color": record.get("color"),
            "description": record.get("description"),
            "vm_role": record.get("vm_role"),
            "tags": record.get("tags"),
        },
    )
    context.vm_role_cache[vm_type_key] = role
    return role


async def _resolve_vm_task_device(
    context: _VMCreateContext,
    *,
    endpoint_id: int | None,
    cluster_name: str,
    resource: dict,
    cluster_source: object | None,
    px_source: object | None,
    cluster: object,
    cluster_dependencies: dict[str, object],
    site_id: int | None,
) -> object:
    node_name = str(resource.get("node"))
    device_key = (endpoint_id, cluster_name, node_name)
    device = context.node_device_cache.get(device_key)
    if device is not None:
        return device
    device = await _ensure_device(
        context.nb,
        device_name=node_name,
        **_endpoint_node_naming_kwargs(
            cluster_source or px_source,
            cluster_name,
            endpoint_id,
        ),
        cluster_id=getattr(cluster, "id", None),
        device_type_id=getattr(cluster_dependencies.get("device_type"), "id", None),
        role_id=getattr(cluster_dependencies.get("device_role"), "id", None),
        site_id=site_id,
        tag_refs=context.tag_refs,
        overwrite_device_role=context.overwrite_flags.overwrite_device_role,
        overwrite_device_type=context.overwrite_flags.overwrite_device_type,
        overwrite_device_tags=context.overwrite_flags.overwrite_device_tags,
        overwrite_flags=context.overwrite_flags,
    )
    context.node_device_cache[device_key] = device
    return device


async def _resolve_vm_task_dependencies(
    context: _VMCreateContext,
    *,
    cluster_name: str,
    resource: dict,
    endpoint_id: int | None,
    px_source: object | None,
    cluster_source: object | None,
    vm_type_key: str,
    vm_name: str,
    timing_key: str,
) -> _VMTaskDependencies:
    try:
        await _emit_vm_dependency_start(context, vm_name)
        cluster_dependencies = context.cluster_dependency_cache.get((endpoint_id, cluster_name), {})
        cluster = cluster_dependencies.get("cluster")
        if cluster is None:
            raise ProxboxException(
                message=(
                    "Error creating Virtual Machine dependent objects "
                    "(cluster, device, tag and role)"
                ),
                python_exception=(
                    f"Missing precomputed cluster dependency for cluster={cluster_name}"
                ),
            )
        site_id = _cluster_dependency_site_id(cluster_dependencies)
        device = await _resolve_vm_task_device(
            context,
            endpoint_id=endpoint_id,
            cluster_name=cluster_name,
            resource=resource,
            cluster_source=cluster_source,
            px_source=px_source,
            cluster=cluster,
            cluster_dependencies=cluster_dependencies,
            site_id=site_id,
        )
        role = await _resolve_vm_task_role(context, vm_type_key)
        vm_type = await _resolve_cached_vm_type(context, vm_type_key)
        logger.debug("VM deps cluster=%s device=%s role=%s", cluster, device, role)
        await _emit_vm_dependency_complete(context, vm_name, timing_key)
        return _VMTaskDependencies(
            cluster=cluster,
            device=device,
            role=role,
            vm_type_id=int(getattr(vm_type, "id", 0) or 0) if vm_type else None,
            site_id=site_id,
            cluster_dependencies=cluster_dependencies,
        )
    except Exception as error:
        await _raise_vm_dependency_error(context, vm_name, error)
        raise AssertionError("unreachable")


async def _emit_vm_dependency_start(context: _VMCreateContext, vm_name: str) -> None:
    if context.bridge is not None:
        await context.bridge.emit_substep(
            phase="virtual-machines",
            substep="resolve_dependencies",
            status=SubstepStatus.PROCESSING,
            message=f"Resolving dependencies for VM '{vm_name}'",
            item={"name": vm_name},
        )


async def _emit_vm_dependency_complete(
    context: _VMCreateContext,
    vm_name: str,
    timing_key: str,
) -> None:
    if context.bridge is not None:
        await context.bridge.emit_substep(
            phase="virtual-machines",
            substep="resolve_dependencies",
            status=SubstepStatus.COMPLETED,
            message=f"Dependencies ready for VM '{vm_name}'",
            item={"name": vm_name},
            timing_key=timing_key,
        )


async def _raise_vm_dependency_error(
    context: _VMCreateContext,
    vm_name: str,
    error: Exception,
) -> None:
    detail = _vm_dependency_error_detail(error)
    if context.bridge is not None:
        await context.bridge.emit_error_detail(
            message="Failed to resolve VM dependencies",
            category=ErrorCategory.VALIDATION,
            phase="virtual-machines",
            item={"name": vm_name},
            detail=detail,
            suggestion="Check cluster, node device, and VM role mappings in NetBox",
        )
    raise ProxboxException(
        message=(
            "Error creating Virtual Machine dependent objects (cluster, device, tag and role)"
        ),
        detail=detail,
        python_exception=f"Error: {detail}",
    )


async def _build_vm_task_payload(
    context: _VMCreateContext,
    *,
    cluster_name: str,
    resource: dict,
    vm_config: dict,
    dependencies: _VMTaskDependencies,
    vm_type_key: str,
    px_source: object | None,
) -> dict:
    proxmox_tag_ids = await _resolve_vm_proxmox_tag_ids(context, cluster_name, vm_config)
    proxbox_tag_id = int(getattr(context.tag, "id", 0) or 0)
    platform_id = await _resolve_vm_platform_id(
        context.nb,
        px_session=px_source,
        node=str(resource.get("node") or ""),
        vmid=int(resource.get("vmid") or 0),
        resource=resource,
        vm_config=vm_config if isinstance(vm_config, dict) else {},
        tag_refs=context.tag_refs,
        refine_from_guest_agent=bool(context.behavior_flags.sync_vm_platform_from_guest_agent),
        cache=context.platform_cache,
    )
    payload = build_netbox_virtual_machine_payload(
        proxmox_resource=resource,
        proxmox_config=vm_config,
        cluster_id=int(getattr(dependencies.cluster, "id", 0) or 0),
        device_id=int(getattr(dependencies.device, "id", 0) or 0),
        role_id=(
            None if dependencies.vm_type_id else int(getattr(dependencies.role, "id", 0) or 0)
        ),
        tag_ids=sorted({proxbox_tag_id, *proxmox_tag_ids} - {0}),
        site_id=dependencies.site_id,
        tenant_id=(
            int(getattr(dependencies.cluster_dependencies.get("tenant"), "id", 0) or 0) or None
        ),
        virtual_machine_type_id=dependencies.vm_type_id,
        platform_id=platform_id,
        parse_description_metadata=context.behavior_flags.parse_description_metadata,
        overwrite_flags=context.effective_vm_overwrite_flags,
    )
    resolved_name = context.default_resolved_vm_names.get(
        _vm_task_result_key(cluster_name, resource)
    )
    if resolved_name:
        payload["name"] = resolved_name
    return payload


def _vm_task_result_key(cluster_name: str, resource: dict) -> tuple[str, int, str]:
    try:
        vmid = int(resource.get("vmid") or 0)
    except (TypeError, ValueError):
        vmid = 0
    return (
        cluster_name,
        vmid,
        str(resource.get("type") or "").strip().lower(),
    )


async def _emit_vm_reconcile_substep(
    context: _VMCreateContext,
    vm_name: str,
    timing_key: str,
    *,
    completed: bool,
) -> None:
    if context.bridge is None:
        return
    await context.bridge.emit_substep(
        phase="virtual-machines",
        substep="reconcile_vm",
        status=SubstepStatus.COMPLETED if completed else SubstepStatus.PROCESSING,
        message=(
            f"VM '{vm_name}' reconciled in NetBox"
            if completed
            else f"Reconciling VM '{vm_name}' in NetBox"
        ),
        item={"name": vm_name},
        **({"timing_key": timing_key} if completed else {}),
    )


async def _reconcile_vm_task_record(
    context: _VMCreateContext,
    *,
    cluster_name: str,
    resource: dict,
    vm_config: dict,
    dependencies: _VMTaskDependencies,
    payload: dict,
    endpoint_id: int | None,
    vm_name: str,
    timing_key: str,
    now: datetime,
) -> dict:
    await _emit_vm_reconcile_substep(context, vm_name, timing_key, completed=False)
    existing_resolution = await resolve_virtual_machine_by_sync_state(
        context.nb,
        proxmox_vm_id=resource.get("vmid"),
        endpoint_id=endpoint_id,
        cluster_id=int(getattr(dependencies.cluster, "id", 0) or 0) or None,
        fail_on_ambiguous=True,
    )
    existing_record = (
        _to_mapping(existing_resolution.record) if existing_resolution is not None else None
    )
    snapshot_read = resolve_snapshot_read_from_scan(
        context.network_role_snapshot_scan,
        existing_record,
    )
    payload, patchable_fields, role_decision = apply_role_snapshot_policy(
        existing_record=existing_record,
        existing_snapshot_id=snapshot_read.snapshot_id,
        desired_payload=payload,
        patchable_fields=context.vm_patchable_fields,
        overwrite_vm_role=context.overwrite_vm_role,
        snapshot_read_verified=snapshot_read.verified,
    )
    virtual_machine = await rest_reconcile_async(
        context.nb,
        "/api/virtualization/virtual-machines/",
        lookup=_missing_vm_lookup(),
        payload=payload,
        schema=NetBoxVirtualMachineCreateBody,
        patchable_fields=patchable_fields,
        current_normalizer=lambda record: _normalize_current_virtual_machine_payload(
            record,
            supports_virtual_machine_type_field=context.supports_vm_type,
        ),
        strict_lookup=True,
        existing_record=(existing_resolution.record if existing_resolution is not None else None),
    )
    virtual_machine_id = (
        virtual_machine.get("id")
        if isinstance(virtual_machine, dict)
        else getattr(virtual_machine, "id", None)
    )
    await _persist_network_vm_state(
        context,
        cluster_name=cluster_name,
        resource=resource,
        vm_config=vm_config,
        now=now,
        virtual_machine_id=virtual_machine_id,
        existing_record=existing_record,
        snapshot_read=snapshot_read,
        role_decision=role_decision,
        endpoint_id=endpoint_id,
    )
    await stamp_vm_last_run_id(context.nb, virtual_machine, context.effective_run_id)
    logger.debug("Reconciled virtual_machine=%s", virtual_machine)
    await _emit_vm_reconcile_substep(context, vm_name, timing_key, completed=True)
    if not isinstance(virtual_machine, dict):
        virtual_machine = virtual_machine.dict()
    return virtual_machine


async def _persist_network_vm_state(
    context: _VMCreateContext,
    *,
    cluster_name: str,
    resource: dict,
    vm_config: dict,
    now: datetime,
    virtual_machine_id: object,
    existing_record: dict | None,
    snapshot_read: object,
    role_decision: RoleSnapshotDecision,
    endpoint_id: int | None,
) -> None:
    sync_state_fields = build_virtual_machine_sync_state_fields(
        proxmox_resource=resource,
        proxmox_config=vm_config,
        last_updated=now,
        cluster_name=cluster_name,
        proxmox_url=(
            context.proxmox_url_by_cluster.get((endpoint_id, cluster_name))
            or context.proxmox_url_by_cluster.get(cluster_name)
        ),
        endpoint_id=endpoint_id,
    )
    await persist_sync_state_with_role_compensation(
        context.nb,
        persistence=write_virtual_machine_sync_state(
            context.nb,
            virtual_machine_id=virtual_machine_id,
            custom_fields=sync_state_fields,
            overwrite_custom_fields=context.overwrite_vm_custom_fields,
            proxmox_vm_name=resource.get("name"),
            proxmox_last_synced_role_id=(
                role_decision.snapshot_value if role_decision.write_snapshot else None
            ),
        ),
        virtual_machine_id=(
            int(cast("int | str", virtual_machine_id)) if virtual_machine_id is not None else None
        ),
        previous_role_id=(
            _relation_id(existing_record.get("role")) if existing_record is not None else None
        ),
        previous_snapshot_id=snapshot_read.snapshot_id,
        expected_snapshot_id=(
            role_decision.snapshot_value if role_decision.write_snapshot else None
        ),
        role_write_applied=role_decision.write_role,
    )


async def _vm_task_guest_interfaces(
    context: _VMCreateContext,
    *,
    cluster_name: str,
    resource: dict,
    vm_type: object,
    vm_config_obj: ProxmoxVmConfigInput,
    vm_name: str,
    timing_key: str,
) -> list[dict]:
    if vm_type != "qemu" or not vm_config_obj.qemu_agent_enabled:
        return []
    proxmox_session = next(
        (
            px
            for px, cluster in zip(context.pxs, context.cluster_status)
            if getattr(cluster, "name", None) == cluster_name
        ),
        None,
    )
    if proxmox_session is None:
        return []
    result = await fetch_qemu_guest_agent_network_interfaces(
        proxmox_session,
        node=str(resource.get("node")),
        vmid=int(resource.get("vmid")),
    )
    if result.interfaces:
        return result.interfaces
    logger.info(
        "Guest agent network data unavailable for VM %s (vmid=%s); "
        "falling back to config networks. (%s)",
        resource.get("name"),
        resource.get("vmid"),
        result.diagnostic or "no interfaces returned",
    )
    if context.bridge is not None and result.diagnostic:
        await context.bridge.emit_substep(
            phase="virtual-machines",
            substep="vm_interfaces",
            status=SubstepStatus.COMPLETED,
            message=f"VM '{vm_name}' guest-agent IPs unavailable: {result.diagnostic}",
            item={"name": vm_name, "vmid": resource.get("vmid")},
            timing_key=timing_key,
        )
    return []


async def _vm_task_interface_kwargs(
    context: _VMCreateContext,
    *,
    cluster_name: str,
    resource: dict,
    vm_config: dict,
    virtual_machine: dict,
    device: object,
    now: datetime,
    guest_interfaces: list[dict],
) -> list[dict]:
    guest_by_name = {
        str(interface.get("name", "")).strip().lower(): interface for interface in guest_interfaces
    }
    guest_by_mac = build_guest_mac_index(guest_interfaces)
    dns_name = await _resolve_vm_dns_name(
        proxmox_session=next(
            (
                px
                for px, cluster in zip(context.pxs, context.cluster_status)
                if getattr(cluster, "name", None) == cluster_name
            ),
            None,
        ),
        node=str(resource.get("node") or "") or None,
        vmid=resource.get("vmid"),
        vm_type=resource.get("type", "unknown"),
        vm_config=vm_config,
    )
    kwargs_list: list[dict] = []
    for network in _parse_vm_networks(vm_config):
        for interface_name, value in network.items():
            kwargs_list.append(
                _one_vm_task_interface_kwargs(
                    context,
                    virtual_machine=virtual_machine,
                    interface_name=interface_name,
                    value=value,
                    guest_by_name=guest_by_name,
                    guest_by_mac=guest_by_mac,
                    device=device,
                    now=now,
                    dns_name=dns_name,
                )
            )
    return kwargs_list


def _one_vm_task_interface_kwargs(
    context: _VMCreateContext,
    *,
    virtual_machine: dict,
    interface_name: str,
    value: dict,
    guest_by_name: dict[str, dict],
    guest_by_mac: dict,
    device: object,
    now: datetime,
    dns_name: str | None,
) -> dict:
    config_name = str(value.get("name", interface_name)).strip() or interface_name
    interface_mac = value.get("virtio", value.get("hwaddr", None))
    guest_interface = (
        merged_guest_iface_from_mac_index(guest_by_mac, interface_mac) if interface_mac else None
    )
    if guest_interface is None:
        guest_interface = guest_by_name.get(config_name.lower())
    resolved_name = config_name
    if (
        should_use_guest_agent_core_interface_name(
            context.use_guest_agent_interface_name,
            context.normalized_interface_strategy,
        )
        and guest_interface
    ):
        resolved_name = str(guest_interface.get("name") or "").strip() or resolved_name
    return {
        "nb": context.nb,
        "virtual_machine": virtual_machine,
        "interface_name": resolved_name,
        "interface_config": value,
        "guest_iface": guest_interface,
        "tag_refs": context.tag_refs,
        "use_guest_agent_interface_name": context.use_guest_agent_interface_name,
        "ignore_ipv6_link_local_addresses": context.ignore_ipv6_link_local_addresses,
        "primary_ip_preference": context.primary_ip_preference,
        "now": now,
        "device": device,
        "overwrite_flags": context.overwrite_flags,
        "dns_name": dns_name,
        "create_ip": context.assign_vm_interface_ips,
        "sync_mac": context.sync_vm_interface_macs,
        "vm_interface_sync_strategy": context.normalized_interface_strategy,
    }


async def _create_vm_interface_with_retry(
    kwargs: dict,
    attempts: int = 2,
):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _create_vm_interface_parallel(**kwargs)
        except Exception as error:
            last_error = error
            if attempt < attempts:
                logger.warning(
                    "Interface %r creation failed (attempt %d/%d): %s -- retrying",
                    kwargs.get("interface_name"),
                    attempt,
                    attempts,
                    getattr(error, "detail", str(error)),
                )
                await asyncio.sleep(0.5 * attempt)
    assert last_error is not None
    return last_error


async def _run_vm_task_interface_batches(interface_kwargs: list[dict]) -> list:
    from proxbox_api.routes.virtualization.virtual_machines.helpers import (
        resolve_interface_batch_delay_ms,
        resolve_interface_batch_size,
    )

    batch_size = resolve_interface_batch_size()
    batch_delay_ms = resolve_interface_batch_delay_ms()
    results = []
    for index in range(0, len(interface_kwargs), batch_size):
        batch_results = await asyncio.gather(
            *[
                _create_vm_interface_with_retry(kwargs)
                for kwargs in interface_kwargs[index : index + batch_size]
            ],
            return_exceptions=True,
        )
        results.extend(batch_results)
        if index + batch_size < len(interface_kwargs) and batch_delay_ms > 0:
            await asyncio.sleep(batch_delay_ms / 1000.0)
    return results


def _consume_vm_task_interface_results(
    resource: dict,
    interface_results: list,
    total_interfaces: int,
) -> tuple[_VMNetworkResult, list[dict]]:
    result = _VMNetworkResult(total_interfaces=total_interfaces)
    netbox_interfaces: list[dict] = []
    for interface_result in interface_results:
        if isinstance(interface_result, Exception):
            result.failed_interfaces += 1
            detail = getattr(interface_result, "detail", str(interface_result))
            logger.warning(
                "Interface creation failed: %s: %s",
                type(interface_result).__name__,
                detail,
            )
            continue
        if interface_result.get("interface"):
            netbox_interfaces.append(interface_result["interface"])
        for ip_entry in interface_result.get("all_ips") or []:
            ip_id = ip_entry.get("id")
            address = ip_entry.get("address") or ""
            if not ip_id:
                continue
            if ":" in address and result.first_ipv6_id is None:
                result.first_ipv6_id = ip_id
            elif ":" not in address and result.first_ipv4_id is None:
                result.first_ipv4_id = ip_id
    if result.failed_interfaces:
        logger.error(
            "VM %s (vmid=%s): %d of %d interface(s) failed to sync; "
            "the synchronization is degraded, not complete.",
            resource.get("name"),
            resource.get("vmid"),
            result.failed_interfaces,
            result.total_interfaces,
        )
    return result, netbox_interfaces


def _vm_task_guest_reconcile_indexes(
    interface_results: list,
) -> tuple[dict[str, int], dict[int, dict[str, int]]]:
    core_interface_id_by_mac: dict[str, int] = {}
    ip_ids_by_interface_id: dict[int, dict[str, int]] = {}
    for interface_result in interface_results:
        if isinstance(interface_result, Exception):
            continue
        interface_id = interface_result.get("interface_id")
        mac_address = interface_result.get("mac_address")
        if interface_id is not None and mac_address:
            try:
                core_interface_id_by_mac[str(mac_address)] = int(interface_id)
            except (TypeError, ValueError):
                pass
        try:
            interface_id_int = int(interface_id)
        except (TypeError, ValueError):
            continue
        ip_map = ip_ids_by_interface_id.setdefault(interface_id_int, {})
        _add_vm_task_ip_ids(ip_map, interface_result.get("all_ips") or [])
    return core_interface_id_by_mac, ip_ids_by_interface_id


def _add_vm_task_ip_ids(ip_map: dict[str, int], ip_entries: list) -> None:
    for ip_entry in ip_entries:
        if not isinstance(ip_entry, dict):
            continue
        ip_id = ip_entry.get("id")
        address = ip_entry.get("address")
        if ip_id is None or not address:
            continue
        try:
            ip_map[str(address)] = int(ip_id)
        except (TypeError, ValueError):
            continue


async def _sync_vm_task_interfaces(
    context: _VMCreateContext,
    *,
    cluster_name: str,
    resource: dict,
    vm_config: dict,
    vm_config_obj: ProxmoxVmConfigInput,
    virtual_machine: dict,
    device: object,
    now: datetime,
    vm_name: str,
    timing_key: str,
) -> _VMNetworkResult:
    guest_interfaces = await _vm_task_guest_interfaces(
        context,
        cluster_name=cluster_name,
        resource=resource,
        vm_type=resource.get("type", "unknown"),
        vm_config_obj=vm_config_obj,
        vm_name=vm_name,
        timing_key=timing_key,
    )
    interface_kwargs = await _vm_task_interface_kwargs(
        context,
        cluster_name=cluster_name,
        resource=resource,
        vm_config=vm_config,
        virtual_machine=virtual_machine,
        device=device,
        now=now,
        guest_interfaces=guest_interfaces,
    )
    if not interface_kwargs:
        return _VMNetworkResult()
    interface_results = await _run_vm_task_interface_batches(interface_kwargs)
    network_result, _netbox_interfaces = _consume_vm_task_interface_results(
        resource, interface_results, len(interface_kwargs)
    )
    core_ids, ip_ids = _vm_task_guest_reconcile_indexes(interface_results)
    await reconcile_guest_vm_interfaces(
        context.nb,
        int(virtual_machine["id"]),
        guest_interfaces,
        core_ids,
        ip_ids,
        context.tag_refs,
        context.normalized_interface_strategy,
    )
    return network_result


async def _sync_vm_task_disks(
    context: _VMCreateContext,
    *,
    cluster_name: str,
    virtual_machine: dict,
    vm_config_obj: ProxmoxVmConfigInput,
    now: datetime,
) -> None:
    tasks = [
        _create_vm_disk_parallel(
            nb=context.nb,
            virtual_machine=virtual_machine,
            disk_entry=disk_entry,
            cluster_name=cluster_name,
            storage_index=context.storage_index,
            tag_refs=context.tag_refs,
            now=now,
        )
        for disk_entry in vm_config_obj.disks
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _sync_vm_task_children(
    context: _VMCreateContext,
    *,
    cluster_name: str,
    resource: dict,
    vm_config: dict,
    vm_config_obj: ProxmoxVmConfigInput,
    virtual_machine: dict,
    device: object,
    now: datetime,
    vm_name: str,
    timing_key: str,
) -> _VMNetworkResult:
    if not virtual_machine or not vm_config:
        return _VMNetworkResult()
    network_result = await _sync_vm_task_interfaces(
        context,
        cluster_name=cluster_name,
        resource=resource,
        vm_config=vm_config,
        vm_config_obj=vm_config_obj,
        virtual_machine=virtual_machine,
        device=device,
        now=now,
        vm_name=vm_name,
        timing_key=timing_key,
    )
    await _sync_vm_task_disks(
        context,
        cluster_name=cluster_name,
        virtual_machine=virtual_machine,
        vm_config_obj=vm_config_obj,
        now=now,
    )
    return network_result


async def _assign_vm_task_primary_ips(
    context: _VMCreateContext,
    resource: dict,
    virtual_machine: dict,
    network_result: _VMNetworkResult,
) -> None:
    from proxbox_api.services.sync.vm_network import set_primary_ip

    ip_ids = (network_result.first_ipv4_id, network_result.first_ipv6_id)
    if not any(ip_id is not None for ip_id in ip_ids):
        await _report_missing_vm_task_primary_ip(context, resource, virtual_machine)
        return
    for primary_ip_id in ip_ids:
        if primary_ip_id is None:
            continue
        primary_set = await set_primary_ip(
            nb=context.nb,
            virtual_machine=virtual_machine,
            primary_ip_id=primary_ip_id,
            primary_ip_preference=context.primary_ip_preference,
        )
        if not primary_set and context.websocket:
            await context.websocket.send_json(
                {
                    "object": "virtual_machine",
                    "data": {
                        "error": "Could not set primary IP.",
                        "rowid": virtual_machine.get("name"),
                    },
                }
            )


async def _report_missing_vm_task_primary_ip(
    context: _VMCreateContext,
    resource: dict,
    virtual_machine: dict,
) -> None:
    logger.info(
        "No IP available for VM %s (vmid=%s), skipping primary IP assignment.",
        resource.get("name"),
        resource.get("vmid"),
    )
    if context.websocket:
        await context.websocket.send_json(
            {
                "object": "virtual_machine",
                "data": {
                    "completed": True,
                    "status": "warning",
                    "warning": "No IP address found; primary IP not set.",
                    "rowid": virtual_machine.get("name"),
                },
            }
        )


async def _emit_vm_task_complete(
    context: _VMCreateContext,
    *,
    cluster_name: str,
    resource: dict,
    vm_name: str,
    virtual_machine: dict,
    network_result: _VMNetworkResult,
    timing_key: str,
) -> None:
    if context.bridge is None:
        return
    if network_result.failed_interfaces:
        status = "warning"
        message = (
            f"Synced VM '{vm_name}' with degraded interfaces: "
            f"{network_result.failed_interfaces} of {network_result.total_interfaces} "
            "interface(s) failed"
        )
    else:
        status = "completed"
        message = f"Synced VM '{vm_name}'"
    await context.bridge.emit_item_progress(
        phase="virtual-machines",
        item={
            "name": vm_name,
            "type": str(resource.get("type") or "unknown"),
            "cluster": cluster_name,
            "node": str(resource.get("node") or ""),
            "netbox_id": virtual_machine.get("id"),
            "netbox_url": virtual_machine.get("display_url"),
            "failed_interfaces": network_result.failed_interfaces,
            "total_interfaces": network_result.total_interfaces,
        },
        operation=ItemOperation.CREATED,
        status=status,
        message=message,
        progress_current=0,
        progress_total=0,
        timing_key=timing_key,
    )
    context.bridge.clear_timer(timing_key)


async def _create_network_vm_task(
    context: _VMCreateContext,
    cluster_name: object,
    resource: dict,
    endpoint_id: int | None,
    px_source: object | None,
    cluster_source: object | None,
) -> dict:
    cluster_name_text = str(cluster_name)
    vm_type_key = str(resource.get("type") or "undefined").lower()
    if vm_type_key not in VM_ROLE_MAPPINGS:
        vm_type_key = "undefined"
    vm_config, vm_config_obj = await _network_vm_config(context, resource, px_source)
    vm_name = str(resource.get("name") or resource.get("vmid") or "unknown")
    timing_key = f"vm_{cluster_name}_{resource.get('vmid')}"
    await _emit_vm_task_start(context, cluster_name, resource, vm_name, timing_key)
    dependencies = await _resolve_vm_task_dependencies(
        context,
        cluster_name=cluster_name_text,
        resource=resource,
        endpoint_id=endpoint_id,
        px_source=px_source,
        cluster_source=cluster_source,
        vm_type_key=vm_type_key,
        vm_name=vm_name,
        timing_key=timing_key,
    )
    now = datetime.now(timezone.utc)
    payload = await _build_vm_task_payload(
        context,
        cluster_name=cluster_name_text,
        resource=resource,
        vm_config=vm_config,
        dependencies=dependencies,
        vm_type_key=vm_type_key,
        px_source=px_source,
    )
    virtual_machine = await _reconcile_vm_task_record(
        context,
        cluster_name=cluster_name_text,
        resource=resource,
        vm_config=vm_config,
        dependencies=dependencies,
        payload=payload,
        endpoint_id=endpoint_id,
        vm_name=vm_name,
        timing_key=timing_key,
        now=now,
    )
    network_result = await _sync_vm_task_children(
        context,
        cluster_name=cluster_name_text,
        resource=resource,
        vm_config=vm_config,
        vm_config_obj=vm_config_obj,
        virtual_machine=virtual_machine,
        device=dependencies.device,
        now=now,
        vm_name=vm_name,
        timing_key=timing_key,
    )
    context.required_suboperation_failures += int(network_result.failed_interfaces > 0)
    await _assign_vm_task_primary_ips(context, resource, virtual_machine, network_result)
    await _emit_vm_task_complete(
        context,
        cluster_name=cluster_name_text,
        resource=resource,
        vm_name=vm_name,
        virtual_machine=virtual_machine,
        network_result=network_result,
        timing_key=timing_key,
    )
    return virtual_machine


async def _run_network_vm_task(
    context: _VMCreateContext,
    semaphore: asyncio.Semaphore,
    cluster_name: object,
    resource: dict,
    endpoint_id: int | None,
    px_source: object | None,
    cluster_source: object | None,
) -> dict:
    async with semaphore:
        return await _create_network_vm_task(
            context,
            cluster_name,
            resource,
            endpoint_id,
            px_source,
            cluster_source,
        )


async def _create_cluster_network_vms(
    context: _VMCreateContext,
    semaphore: asyncio.Semaphore,
    cluster: dict,
    cluster_index: int,
) -> list:
    endpoint_id = context.ordered_endpoint_ids[cluster_index]
    px_source = context.px_list[cluster_index] if cluster_index < len(context.px_list) else None
    cluster_source = (
        context.cluster_status_list[cluster_index]
        if cluster_index < len(context.cluster_status_list)
        else None
    )
    tasks = [
        _run_network_vm_task(
            context,
            semaphore,
            cluster_name,
            resource,
            endpoint_id,
            px_source,
            cluster_source,
        )
        for cluster_name, resources in cluster.items()
        for resource in resources
        if resource.get("type") in ("qemu", "lxc")
    ]
    return await asyncio.gather(*tasks, return_exceptions=True)


def _count_network_vm_inputs(filtered_cluster_resources: list) -> int:
    total_count = 0
    for cluster in filtered_cluster_resources:
        cluster_name = list(cluster.keys())[0]
        resources = cluster[cluster_name]
        total_count += len(
            [resource for resource in resources if resource.get("type") in ("qemu", "lxc")]
        )
    return total_count


def _flatten_network_vm_results(
    result_list: list,
) -> tuple[list[dict], int]:
    flattened_results: list[dict] = []
    failed_count = 0
    for cluster_result in result_list:
        if isinstance(cluster_result, Exception):
            failed_count += 1
            continue
        for vm_result in cluster_result:
            if isinstance(vm_result, Exception):
                failed_count += 1
            else:
                flattened_results.append(vm_result)
    return flattened_results, failed_count


def _log_network_vm_task_failures(result_list: list) -> None:
    logger.debug("VM creation gather complete: %d cluster result(s)", len(result_list))
    for cluster_result in result_list:
        if isinstance(cluster_result, Exception):
            continue
        for result in cluster_result:
            if isinstance(result, Exception):
                logger.warning(
                    "VM sub-task failed: %s",
                    getattr(result, "python_exception", str(result)),
                )


async def _run_network_vm_sync(context: _VMCreateContext) -> object:
    try:
        total_count = _count_network_vm_inputs(context.filtered_cluster_resources)
        semaphore = asyncio.Semaphore(resolve_vm_sync_concurrency())
        result_list = await asyncio.gather(
            *[
                _create_cluster_network_vms(context, semaphore, cluster, index)
                for index, cluster in enumerate(context.filtered_cluster_resources)
            ],
            return_exceptions=True,
        )
        _log_network_vm_task_failures(result_list)
        results, failed_count = _flatten_network_vm_results(result_list)
        await _sync_scoped_vm_task_history(context, results)
        await _emit_vm_sync_summary(
            context,
            len(results),
            failed_count,
            concise_success=False,
        )
        _log_vm_sync_summary(total_count, len(results), failed_count)
    except ProxboxException:
        raise
    except Exception as error:
        await _raise_network_vm_sync_error(context, error)
        raise AssertionError("unreachable")
    return _vm_stage_result(
        results,
        context.selection_skipped,
        failed_count=failed_count + context.required_suboperation_failures,
    )


async def _raise_network_vm_sync_error(
    context: _VMCreateContext,
    error: Exception,
) -> None:
    if context.bridge is not None:
        await context.bridge.emit_error_detail(
            message="Virtual machine sync failed",
            category=ErrorCategory.INTERNAL,
            phase="virtual-machines",
            detail=str(error),
            suggestion="Review backend logs and retry the synchronization",
        )
    raise ProxboxException(message=f"Error during VM sync: {str(error)}")


@router.get(
    "/create",
    dependencies=[
        Depends(ensure_netbox_sync_dependencies),
        Depends(reset_sidecar_availability_cache),
    ],
)
async def create_virtual_machines(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_css: bool = False,
    use_websocket: bool = False,
    sync_vm_network: bool = True,
    sync_task_history: bool = Query(
        default=True,
        title="Sync Task History",
        description=(
            "Synchronize archived Proxmox task history after successful VM reconciliation. "
            "Disable when a dedicated task-history stage follows immediately."
        ),
    ),
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "Compatibility toggle for legacy_rename. In guest_os_model, core "
            "VMInterfaces keep Proxmox netX names and guest OS names are written "
            "to plugin guest interface rows."
        ),
    ),
    vm_interface_sync_strategy: Literal["guest_os_model", "legacy_rename"] = Query(
        default="guest_os_model",
        title="VM Interface Sync Strategy",
        description=(
            "guest_os_model keeps core VMInterfaces named from Proxmox config (netX) "
            "and writes guest OS interfaces to the netbox-proxbox plugin. "
            "legacy_rename preserves the deprecated guest-agent rename behavior."
        ),
    ),
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description="Comma-separated list of NetBox VM IDs to sync. When provided, only these VMs will be synced.",
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=(
            "When true, IPv6 link-local addresses (fe80::/64) are ignored during "
            "VM interface IP address selection. Disable only if you need link-local addresses included."
        ),
    ),
    primary_ip_preference: Literal["ipv4", "ipv6"] = Query(
        default="ipv4",
        title="Primary IP Preference",
        description="Preferred IP family when choosing VM primary IP (ipv4 or ipv6).",
    ),
    overwrite_vm_role: bool | None = Query(
        default=None,
        title="Overwrite VM Role",
        description=(
            "When false, the VM role is not patched on existing VMs that already have a role. "
            "The role is still set when a VM is first created. "
            "When unset, falls back to overwrite_flags.overwrite_vm_role."
        ),
    ),
    overwrite_vm_type: bool | None = Query(
        default=None,
        title="Overwrite VM Type",
        description=(
            "When false, the VM type is not patched on existing VMs that already have a type. "
            "The type is still set when a VM is first created. "
            "When unset, falls back to overwrite_flags.overwrite_vm_type."
        ),
    ),
    overwrite_vm_tags: bool | None = Query(
        default=None,
        title="Overwrite VM Tags",
        description=(
            "When false, tags are not patched on existing VMs that already have tags. "
            "Tags are still applied when a VM is first created. "
            "When unset, falls back to overwrite_flags.overwrite_vm_tags."
        ),
    ),
    overwrite_vm_description: bool | None = Query(
        default=None,
        title="Overwrite VM Description",
        description=(
            "When false, the VM description is not patched on existing VMs that already "
            "have a non-empty description. The description is still set on first create. "
            "When unset, falls back to overwrite_flags.overwrite_vm_description."
        ),
    ),
    overwrite_vm_custom_fields: bool | None = Query(
        default=None,
        title="Overwrite VM Sync State",
        description=(
            "Compatibility-named flag controlling typed VM sync-state sidecar updates. "
            "It no longer writes NetBox custom fields. "
            "When unset, falls back to overwrite_flags.overwrite_vm_custom_fields."
        ),
    ),
    overwrite_flags: ResolvedSyncOverwriteFlagsDep = SyncOverwriteFlags(),
    behavior_flags: ResolvedSyncBehaviorFlagsDep = SyncBehaviorFlags(),
    run_id: str | None = Query(
        default=None,
        title="Run ID",
        description=(
            "UUID stored in each touched VM's typed sync-state sidecar. "
            "When omitted, a fresh UUID is generated. Pass the full-update operation_id "
            "to make every VM touched in a single run share the same stamp."
        ),
    ),
    assign_vm_interface_ips: bool = Query(
        default=True,
        title="Assign VM Interface IPs",
        description=(
            "When false, IP address reconciliation is skipped for VM interfaces in this pass. "
            "The VMInterface and MAC address are still created or updated. "
            "Use when a dedicated IP-assignment stage follows separately."
        ),
    ),
    sync_vm_interface_macs: bool = Query(
        default=True,
        title="Sync VM Interface MACs",
        description=(
            "When false, MAC address reconciliation is skipped for VM interfaces in this pass. "
            "The VMInterface and IP addresses are still created or updated. "
            "Use when MAC sync is handled by a separate stage."
        ),
    ),
    sync_mode_vm: str = Query(
        default="always",
        title="Sync Mode (VM)",
        description=(
            "Controls whether non-template VMs are included in this sync pass. "
            "'always' (default): sync all non-template VMs. "
            "'bootstrap_only': treated as enabled at the backend — the plugin manages bootstrap tracking. "
            "'disabled': non-template VMs are skipped entirely in this pass. "
            "Unknown values fall back to 'always' with a warning."
        ),
    ),
    sync_mode_vm_template: str = Query(
        default="always",
        title="Sync Mode (VM Template)",
        description=(
            "Controls whether template VMs are included in this sync pass. "
            "A Proxmox resource is identified as a template when its 'template' field is truthy (1, '1', True). "
            "'always' (default): sync all templates. "
            "'bootstrap_only': treated as enabled at the backend. "
            "'disabled': template VMs are skipped entirely in this pass. "
            "Unknown values fall back to 'always' with a warning."
        ),
    ),
):
    """Create and synchronize virtual machines from Proxmox to NetBox.

    Discovers virtual machines in Proxmox cluster resources and creates or updates
    corresponding NetBox VM objects with network interfaces, disks, and metadata.

    Args:
        netbox_session: NetBox API session for creating/updating VMs.
        pxs: Proxmox session(s) for fetching VM configurations.
        cluster_status: Cluster status objects containing node and resource information.
        cluster_resources: Proxmox cluster resources from Proxmox (VMs, LXC containers).
        tag: ProxBox tag reference for tagging created objects.
        websocket: Optional WebSocket connection for streaming progress updates.
        use_css: Whether to include CSS styling in HTML status responses.
        use_websocket: Whether to send progress updates via WebSocket/SSE.
        sync_vm_network: When False, skip VM interface and IP reconciliation in this pass.
        use_guest_agent_interface_name: Use QEMU guest-agent interface names if available.
        netbox_vm_ids: Comma-separated NetBox VM IDs to filter sync.
        ignore_ipv6_link_local_addresses: Ignore IPv6 link-local addresses when selecting IPs.
        sync_mode_vm: Controls sync behavior for non-template VMs. 'always' syncs all, 'disabled' skips all, 'bootstrap_only' treated as enabled.
        sync_mode_vm_template: Controls sync behavior for template VMs (truthy ``template`` field). Same values as sync_mode_vm.

    Returns:
        HTTP response with creation status, or streaming SSE response if using WebSocket.
    """

    (
        overwrite_vm_role,
        overwrite_vm_type,
        overwrite_vm_tags,
        overwrite_vm_description,
        overwrite_vm_custom_fields,
    ) = _resolve_vm_overwrites(
        overwrite_vm_role,
        overwrite_vm_type,
        overwrite_vm_tags,
        overwrite_vm_description,
        overwrite_vm_custom_fields,
        overwrite_flags,
    )
    effective_overwrite_flags = overwrite_flags.model_copy(
        update={
            "overwrite_vm_role": overwrite_vm_role,
            "overwrite_vm_type": overwrite_vm_type,
            "overwrite_vm_tags": overwrite_vm_tags,
            "overwrite_vm_description": overwrite_vm_description,
            "overwrite_vm_custom_fields": overwrite_vm_custom_fields,
        }
    )
    context = await _build_vm_create_context(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        tag=tag,
        websocket=websocket,
        use_css=use_css,
        use_websocket=use_websocket,
        sync_task_history=sync_task_history,
        use_guest_agent_interface_name=use_guest_agent_interface_name,
        vm_interface_sync_strategy=vm_interface_sync_strategy,
        netbox_vm_ids=netbox_vm_ids,
        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
        primary_ip_preference=primary_ip_preference,
        overwrite_vm_role=overwrite_vm_role,
        overwrite_vm_type=overwrite_vm_type,
        overwrite_vm_tags=overwrite_vm_tags,
        overwrite_vm_description=overwrite_vm_description,
        overwrite_vm_custom_fields=overwrite_vm_custom_fields,
        overwrite_flags=overwrite_flags,
        behavior_flags=behavior_flags,
        effective_vm_overwrite_flags=effective_overwrite_flags,
        run_id=run_id,
        assign_vm_interface_ips=assign_vm_interface_ips,
        sync_vm_interface_macs=sync_vm_interface_macs,
        sync_mode_vm=sync_mode_vm,
        sync_mode_vm_template=sync_mode_vm_template,
    )
    await _emit_vm_discovery(context, sync_vm_network)
    await _load_vm_create_storage_index(context)
    try:
        await _precompute_vm_dependencies(context)
    except Exception as error:
        detail = _vm_dependency_error_detail(error)
        raise ProxboxException(
            message=(
                "Error creating Virtual Machine dependent objects (cluster, device, tag and role)"
            ),
            detail=detail,
            python_exception=f"Error: {detail}",
        )

    prepare_context = _vm_preparation_context(context)
    if not sync_vm_network:
        return await _run_vm_create_without_network(context, prepare_context)

    context.network_role_snapshot_scan = await scan_vm_last_synced_role_ids(context.nb)
    await _resolve_network_vm_names(context)
    return await _run_network_vm_sync(context)


@dataclass(slots=True)
class _VMInterfaceSyncContext:
    nb: object
    pxs: list[object]
    cluster_status: list[object]
    cluster_resources: list[dict]
    websocket: object | None
    use_websocket: bool
    use_guest_agent_interface_name: bool
    overwrite_flags: SyncOverwriteFlags | None
    sync_mac: bool
    normalized_interface_strategy: VMInterfaceSyncStrategy
    tag_refs: list[dict[str, object]]
    now: datetime
    sync_warnings: list[dict[str, object]]
    vm_index: dict
    vm_candidates_by_vmid: dict
    cluster_id_cache: dict[str, int]
    endpoint_id_by_cluster: dict[str, int]
    semaphore: asyncio.Semaphore
    all_interface_payloads: list[dict] = field(default_factory=list)
    all_interface_info: dict[str, dict] = field(default_factory=dict)
    all_vlan_tags: dict[tuple[int, int | None, int | None], list[dict]] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class _VMInterfaceSource:
    cluster_name: str
    endpoint_id: int | None
    resource_node: str
    vm_type: object
    vm_name: str
    netbox_vm: dict
    vm_config: dict[str, object]
    guest_agent_interfaces: list[dict[str, object]]


def _find_vm_resource_by_name_and_type(
    cluster_resources: list[dict],
    cluster_name: str,
    vm_name: str,
    vm_type: object,
) -> dict | None:
    for cluster in cluster_resources:
        if not isinstance(cluster, dict):
            continue
        for candidate_name, resources in cluster.items():
            if str(candidate_name) != cluster_name or not isinstance(resources, list):
                continue
            for resource in resources:
                if resource.get("name", "").strip() == vm_name and resource.get("type") == vm_type:
                    return resource
    return None


def _select_vm_interface_proxmox_session(
    context: _VMInterfaceSyncContext,
    cluster_name: str,
    endpoint_id: int | None,
) -> object | None:
    return next(
        (
            px
            for px, cluster_state in zip(context.pxs, context.cluster_status)
            if getattr(cluster_state, "name", None) == cluster_name
            and (endpoint_id is None or extract_proxmox_session_endpoint_id(px) == endpoint_id)
        ),
        None,
    )


async def _fetch_vm_interface_config(
    context: _VMInterfaceSyncContext,
    *,
    proxmox_session: object | None,
    resource_node: str,
    vm_type: object,
    vmid: object,
    vm_name: str,
) -> dict[str, object]:
    try:
        if not proxmox_session or not resource_node:
            return {}
        result = get_vm_config(
            pxs=[proxmox_session],
            node=resource_node,
            type=vm_type,
            vmid=int(cast("int | str", vmid)),
        )
        if inspect.isawaitable(result):
            result = await result
        return result or {}
    except Exception as exc:
        logger.warning("Could not fetch VM config for %s (vmid=%s): %s", vm_name, vmid, exc)
        return {}


async def _fetch_vm_interface_guest_data(
    proxmox_session: object | None,
    resource_node: str,
    vm_type: object,
    vmid: object,
    vm_config: dict[str, object],
) -> list[dict[str, object]]:
    if vm_type != "qemu" or not _parse_proxmox_kv_flag(vm_config.get("agent")):
        return []
    if not proxmox_session or not resource_node:
        return []
    return (
        await get_qemu_guest_agent_network_interfaces(
            proxmox_session, resource_node, int(cast("int | str", vmid))
        )
        or []
    )


async def _resolve_vm_interface_source(
    context: _VMInterfaceSyncContext,
    cluster_name: str,
    cluster_id: int | None,
    endpoint_id: int | None,
    resource: dict,
) -> _VMInterfaceSource | None:
    cluster_name_text = str(cluster_name)
    resource_node = str(resource.get("node", ""))
    vm_type = resource.get("type", "unknown")
    vm_name = str(resource.get("name", "")).strip()
    vm_record = _find_vm_resource_by_name_and_type(
        context.cluster_resources, cluster_name_text, vm_name, vm_type
    )
    if not vm_record or resource.get("vmid") is None:
        return None

    vmid = resource["vmid"]
    netbox_vm = _resolve_vm_from_index_or_unique_vmid(
        context.vm_index,
        context.vm_candidates_by_vmid,
        endpoint_id=endpoint_id,
        raw_vmid=vmid,
        cluster_name=cluster_name_text,
        sync_context="interface",
        cluster_id=cluster_id,
    )
    if not netbox_vm:
        logger.warning(
            "Skipping VM interface sync for %s (cluster=%s cluster_id=%s vmid=%s): "
            "NetBox VM not found",
            vm_name,
            cluster_name_text,
            cluster_id,
            vmid,
        )
        return None

    proxmox_session = _select_vm_interface_proxmox_session(context, cluster_name_text, endpoint_id)
    vm_config = await _fetch_vm_interface_config(
        context,
        proxmox_session=proxmox_session,
        resource_node=resource_node,
        vm_type=vm_type,
        vmid=vmid,
        vm_name=vm_name,
    )
    guest_agent_interfaces = await _fetch_vm_interface_guest_data(
        proxmox_session, resource_node, vm_type, vmid, vm_config
    )
    return _VMInterfaceSource(
        cluster_name=cluster_name_text,
        endpoint_id=endpoint_id,
        resource_node=resource_node,
        vm_type=vm_type,
        vm_name=vm_name,
        netbox_vm=netbox_vm,
        vm_config=vm_config,
        guest_agent_interfaces=guest_agent_interfaces,
    )


async def _emit_vm_interface_start(
    context: _VMInterfaceSyncContext,
    resolved_name: str,
    vm_name: str,
) -> None:
    if context.use_websocket and context.websocket:
        await context.websocket.send_json(
            {
                "object": "vm_interface",
                "data": {
                    "completed": False,
                    "sync_status": "syncing",
                    "rowid": resolved_name,
                    "name": resolved_name,
                    "vm": vm_name,
                },
            }
        )


def _vm_interface_metadata(
    context: _VMInterfaceSyncContext,
    source: _VMInterfaceSource,
    *,
    resolved_name: str,
    config_dict: dict,
    guest_iface: dict | None,
) -> tuple[dict, dict]:
    payload = {
        "name": resolved_name,
        "enabled": True,
        "bridge": None,
        "untagged_vlan": None,
        "mode": None,
        "tags": context.tag_refs,
        "virtual_machine": source.netbox_vm.get("id"),
    }
    info = {
        "payload": payload,
        "mac_address": config_dict.get("virtio") or config_dict.get("hwaddr"),
        "vlan_tag": config_dict.get("tag"),
        "bridge_name": config_dict.get("bridge"),
        "vm_id": source.netbox_vm.get("id"),
        "resource_node": source.resource_node,
        "cluster_name": source.cluster_name,
        "resolved_name": resolved_name,
        "config_dict": config_dict,
        "guest_iface": guest_iface,
        "guest_agent_interfaces": source.guest_agent_interfaces,
        "vm_name": source.vm_name,
        "site_id": _relation_id(source.netbox_vm.get("site")),
        "tenant_id": _relation_id(source.netbox_vm.get("tenant")),
        "proxmox_endpoint_id": source.endpoint_id,
    }
    return payload, info


async def _build_vm_interface_payloads(
    context: _VMInterfaceSyncContext,
    source: _VMInterfaceSource,
) -> tuple[list[dict], dict[str, dict]]:
    guest_by_name = {
        str(interface.get("name", "")).strip().lower(): interface
        for interface in source.guest_agent_interfaces
    }
    guest_by_mac = build_guest_mac_index(source.guest_agent_interfaces)
    interface_payloads: list[dict] = []
    interface_info: dict[str, dict] = {}

    for network in _parse_vm_networks(source.vm_config):
        for interface_name, config_dict in network.items():
            config_name = str(config_dict.get("name", interface_name)).strip() or interface_name
            interface_mac = config_dict.get("virtio") or config_dict.get("hwaddr")
            guest_iface = (
                merged_guest_iface_from_mac_index(guest_by_mac, interface_mac)
                if interface_mac
                else None
            )
            if guest_iface is None:
                guest_iface = guest_by_name.get(config_name.lower())
            resolved_name = config_name
            if (
                should_use_guest_agent_core_interface_name(
                    context.use_guest_agent_interface_name,
                    context.normalized_interface_strategy,
                )
                and guest_iface
            ):
                resolved_name = str(guest_iface.get("name") or "").strip() or resolved_name
            resolved_name = normalize_vm_interface_name(
                resolved_name,
                fallback=config_name,
                vm_name=source.vm_name,
            )
            await _emit_vm_interface_start(context, resolved_name, source.vm_name)
            try:
                payload, info = _vm_interface_metadata(
                    context,
                    source,
                    resolved_name=resolved_name,
                    config_dict=config_dict,
                    guest_iface=guest_iface,
                )
                interface_info[f"{source.netbox_vm.get('id')}:{resolved_name}"] = info
                interface_payloads.append(payload)
            except Exception as exc:
                await _report_vm_interface_collection_error(
                    context, source.vm_name, resolved_name, exc
                )
    return interface_payloads, interface_info


async def _report_vm_interface_collection_error(
    context: _VMInterfaceSyncContext,
    vm_name: str,
    resolved_name: str,
    exc: Exception,
) -> None:
    detail = getattr(exc, "detail", str(exc))
    logger.warning(
        "Failed to collect interface payload %s for VM %s: %s: %s",
        resolved_name,
        vm_name,
        type(exc).__name__,
        detail,
    )
    if context.use_websocket and context.websocket:
        await context.websocket.send_json(
            {
                "object": "vm_interface",
                "data": {
                    "completed": False,
                    "rowid": resolved_name,
                    "name": resolved_name,
                    "vm": vm_name,
                    "error": str(exc),
                },
            }
        )


async def _sync_one_vm_interfaces(
    context: _VMInterfaceSyncContext,
    cluster_name: str,
    cluster_id: int | None,
    endpoint_id: int | None,
    resource: dict,
) -> tuple[list[dict], dict[str, dict]]:
    source = await _resolve_vm_interface_source(
        context, cluster_name, cluster_id, endpoint_id, resource
    )
    if source is None:
        return [], {}
    return await _build_vm_interface_payloads(context, source)


async def _run_vm_interface_task(
    context: _VMInterfaceSyncContext,
    cluster_name: str,
    cluster_id: int | None,
    endpoint_id: int | None,
    resource: dict,
) -> tuple[list[dict], dict[str, dict]]:
    async with context.semaphore:
        return await _sync_one_vm_interfaces(
            context, cluster_name, cluster_id, endpoint_id, resource
        )


async def _collect_cluster_vm_interface_payloads(
    context: _VMInterfaceSyncContext,
    cluster: dict,
    endpoint_id_hint: int | None,
) -> list:
    tasks = []
    for cluster_name, resources in cluster.items():
        cluster_id = await resolve_netbox_cluster_id_by_name(
            context.nb,
            str(cluster_name),
            cache=context.cluster_id_cache,
        )
        endpoint_id = endpoint_id_hint or context.endpoint_id_by_cluster.get(str(cluster_name))
        for resource in resources:
            if resource.get("type") in ("qemu", "lxc"):
                tasks.append(
                    _run_vm_interface_task(context, cluster_name, cluster_id, endpoint_id, resource)
                )
    return await asyncio.gather(*tasks, return_exceptions=True)


def _collect_vm_interface_vlan_tag(
    context: _VMInterfaceSyncContext,
    info: dict,
) -> None:
    vlan_tag = info.get("vlan_tag")
    if not vlan_tag:
        return
    try:
        key = (
            int(vlan_tag),
            info.get("site_id") if isinstance(info.get("site_id"), int) else None,
            info.get("tenant_id") if isinstance(info.get("tenant_id"), int) else None,
        )
        context.all_vlan_tags.setdefault(key, [])
    except (ValueError, TypeError):
        pass


async def _collect_vm_interface_payloads(context: _VMInterfaceSyncContext) -> None:
    try:
        for cluster_index, cluster in enumerate(context.cluster_resources):
            endpoint_id_hint = _session_endpoint_id_at(context.pxs, cluster_index)
            cluster_results = await _collect_cluster_vm_interface_payloads(
                context, cluster, endpoint_id_hint
            )
            for cluster_result in cluster_results:
                if isinstance(cluster_result, Exception):
                    continue
                payloads, interface_info = cluster_result
                if not isinstance(payloads, list):
                    continue
                context.all_interface_payloads.extend(payloads)
                context.all_interface_info.update(interface_info)
                for info in interface_info.values():
                    _collect_vm_interface_vlan_tag(context, info)
    except Exception as exc:
        detail = getattr(exc, "detail", str(exc))
        logger.warning(
            "Error during VM interfaces collection: %s: %s",
            type(exc).__name__,
            detail,
        )


async def _reconcile_vm_interface_vlans(
    context: _VMInterfaceSyncContext,
) -> dict:
    if not context.all_vlan_tags:
        return {}
    try:
        from proxbox_api.services.sync.network import build_vlan_payload, bulk_reconcile_vlans

        vlan_payloads = [
            build_vlan_payload(
                vid,
                context.tag_refs,
                context.now,
                site_id=site_id,
                tenant_id=tenant_id,
            )
            for vid, site_id, tenant_id in context.all_vlan_tags
        ]
        result = await bulk_reconcile_vlans(context.nb, vlan_payloads)
        logger.info("Bulk VLAN reconciliation completed: %d VLANs processed", len(vlan_payloads))
        return result
    except Exception as exc:
        logger.error("Error during VLAN bulk reconciliation: %s", exc)
        return {}


def _apply_vm_interface_vlan_ids(context: _VMInterfaceSyncContext, vlan_ids: dict) -> None:
    for info in context.all_interface_info.values():
        vlan_tag = info.get("vlan_tag")
        if not vlan_tag:
            continue
        try:
            vid = int(vlan_tag)
            key = (
                vid,
                info.get("site_id") if isinstance(info.get("site_id"), int) else None,
                info.get("tenant_id") if isinstance(info.get("tenant_id"), int) else None,
            )
            vlan_id = vlan_ids.get(key) or vlan_ids.get(vid)
            if vlan_id is not None:
                info["payload"]["untagged_vlan"] = vlan_id
                info["payload"]["mode"] = "access"
        except (ValueError, TypeError):
            pass


async def _report_partial_vm_interface_reconcile(
    context: _VMInterfaceSyncContext,
    successful_count: int,
) -> None:
    failed_count = max(len(context.all_interface_payloads) - successful_count, 0)
    if not failed_count:
        return
    message = (
        "VM interface reconciliation completed with partial failures: "
        f"{successful_count} succeeded, {failed_count} failed."
    )
    context.sync_warnings.append(
        {
            "phase": "vm-interfaces",
            "succeeded": successful_count,
            "failed": failed_count,
            "requested": len(context.all_interface_payloads),
            "message": message,
        }
    )
    logger.warning(message)
    if (
        context.use_websocket
        and context.websocket
        and hasattr(context.websocket, "emit_phase_summary")
    ):
        await context.websocket.emit_phase_summary(
            phase="vm-interfaces",
            created=successful_count,
            failed=failed_count,
            message=message,
        )


async def _reconcile_vm_interface_macs(
    context: _VMInterfaceSyncContext,
    interface_ids: dict,
) -> None:
    if not context.sync_mac:
        return
    from proxbox_api.services.sync.mac_address import reconcile_mac_for_vm_interface

    for info in context.all_interface_info.values():
        mac_value = info.get("mac_address")
        vm_id = info.get("vm_id")
        interface_name = info.get("resolved_name")
        if not mac_value or not vm_id or not interface_name:
            continue
        interface_id = interface_ids.get((interface_name, int(vm_id)))
        if not interface_id:
            continue
        try:
            await reconcile_mac_for_vm_interface(
                context.nb,
                vminterface_id=int(interface_id),
                mac=mac_value,
                tag_refs=context.tag_refs,
            )
        except Exception as exc:
            logger.warning(
                "Failed to reconcile MAC %s for VM interface %s: %s",
                mac_value,
                interface_id,
                exc,
            )


def _build_guest_vm_interface_contexts(
    context: _VMInterfaceSyncContext,
    interface_ids: dict,
) -> dict[int, dict[str, object]]:
    guest_contexts: dict[int, dict[str, object]] = {}
    for info in context.all_interface_info.values():
        vm_id = info.get("vm_id")
        interface_name = info.get("resolved_name")
        if not vm_id or not interface_name:
            continue
        try:
            vm_id_int = int(vm_id)
        except (TypeError, ValueError):
            continue
        interface_id = interface_ids.get((interface_name, vm_id_int))
        guest_context = guest_contexts.setdefault(
            vm_id_int,
            {
                "guest_interfaces": info.get("guest_agent_interfaces") or [],
                "core_interface_id_by_mac": {},
            },
        )
        if not guest_context.get("guest_interfaces"):
            guest_context["guest_interfaces"] = info.get("guest_agent_interfaces") or []
        mac_value = info.get("mac_address")
        if mac_value and interface_id:
            cast("dict[str, int]", guest_context["core_interface_id_by_mac"])[str(mac_value)] = int(
                interface_id
            )
    return guest_contexts


async def _reconcile_guest_vm_interface_contexts(
    context: _VMInterfaceSyncContext,
    interface_ids: dict,
) -> None:
    for vm_id, guest_context in _build_guest_vm_interface_contexts(context, interface_ids).items():
        await reconcile_guest_vm_interfaces(
            context.nb,
            vm_id,
            cast("list[dict[str, object]]", guest_context.get("guest_interfaces") or []),
            cast(
                "dict[str, int]",
                guest_context.get("core_interface_id_by_mac") or {},
            ),
            {},
            context.tag_refs,
            context.normalized_interface_strategy,
        )


async def _emit_created_vm_interfaces(
    context: _VMInterfaceSyncContext,
    created_interfaces: list[dict],
) -> None:
    if not context.use_websocket or not context.websocket:
        return
    for interface in created_interfaces:
        interface_name = interface.get("name")
        vm_id = interface.get("virtual_machine")
        info = context.all_interface_info.get(f"{vm_id}:{interface_name}")
        if info is None:
            continue
        await context.websocket.send_json(
            {
                "object": "vm_interface",
                "data": {
                    "completed": True,
                    "rowid": interface_name,
                    "name": interface_name,
                    "vm": info.get("vm_name"),
                    "netbox_id": interface.get("id"),
                    "mac_address": interface.get("mac_address"),
                },
            }
        )


async def _report_vm_interface_bulk_failure(
    context: _VMInterfaceSyncContext,
    exc: Exception,
) -> None:
    detail = f"{type(exc).__name__}: {getattr(exc, 'detail', str(exc))}"
    logger.error("Error during interface bulk reconciliation: %s", detail)
    if context.use_websocket and context.websocket:
        await context.websocket.send_json(
            {
                "object": "vm_interface",
                "data": {
                    "completed": False,
                    "sync_status": "failed",
                    "error": detail,
                },
            }
        )
        await context.websocket.send_json({"object": "vm_interface", "end": True})
    raise ProxboxException(
        message=(
            "VM interface bulk reconciliation failed; interface sync is incomplete for this pass."
        ),
        python_exception=detail,
    ) from exc


async def _bulk_reconcile_vm_interfaces(context: _VMInterfaceSyncContext) -> list[dict]:
    if not context.all_interface_payloads:
        return []
    try:
        from proxbox_api.services.sync.network import bulk_reconcile_vm_interfaces

        created_interfaces, interface_ids = await bulk_reconcile_vm_interfaces(
            context.nb,
            context.all_interface_payloads,
            overwrite_flags=context.overwrite_flags,
        )
        logger.info(
            "Bulk interface reconciliation completed: %d interfaces processed",
            len(context.all_interface_payloads),
        )
        await _report_partial_vm_interface_reconcile(context, len(created_interfaces))
        await _reconcile_vm_interface_macs(context, interface_ids)
        await _reconcile_guest_vm_interface_contexts(context, interface_ids)
        await _emit_created_vm_interfaces(context, created_interfaces)
        return [
            {
                "id": interface.get("id"),
                "mac_address": interface.get("mac_address"),
                "interface": interface,
            }
            for interface in created_interfaces
        ]
    except Exception as exc:
        await _report_vm_interface_bulk_failure(context, exc)
        raise AssertionError("unreachable")


async def _write_vm_interface_bridge_state(
    context: _VMInterfaceSyncContext,
    info: dict,
    vm_bridge_id: int,
) -> None:
    from proxbox_api.netbox_rest import rest_first_async

    vm_id = info.get("vm_id")
    resolved_name = info.get("resolved_name", "")
    if not resolved_name:
        return
    try:
        existing_interface = await rest_first_async(
            context.nb,
            "/api/virtualization/interfaces/",
            query={
                "virtual_machine_id": int(vm_id),
                "name": resolved_name,
                "limit": 1,
            },
        )
        if not existing_interface:
            return
        interface_id = (
            existing_interface.get("id")
            if isinstance(existing_interface, dict)
            else getattr(existing_interface, "id", None)
        )
        if interface_id:
            await write_vm_interface_sync_state(
                context.nb,
                vm_interface_id=interface_id,
                proxbox_bridge_id=vm_bridge_id,
                overwrite_custom_fields=(
                    context.overwrite_flags is None
                    or context.overwrite_flags.overwrite_vm_interface_custom_fields
                ),
            )
    except Exception as exc:
        logger.warning(
            "Failed to resolve interface %s (VM %s) for proxbox_bridge: %s",
            resolved_name,
            vm_id,
            exc,
        )


async def _reconcile_one_vm_interface_bridge(
    context: _VMInterfaceSyncContext,
    info: dict,
    node_device_id_cache: dict[tuple[int | None, str, str], int | None],
) -> None:
    from proxbox_api.services.sync.bridge_interfaces import ensure_bridge_interfaces

    bridge_name = info.get("bridge_name")
    vm_id = info.get("vm_id")
    if not bridge_name or not vm_id:
        return
    cluster_name, node_name, site_id, endpoint_id = _bridge_device_lookup_args(info)
    try:
        device_id = (
            await _resolve_bridge_device_id(
                context.nb,
                node_device_id_cache,
                context.cluster_status,
                cluster_name,
                node_name,
                site_id,
                endpoint_id,
            )
            if node_name
            else None
        )
        vm_bridge_id = await ensure_bridge_interfaces(
            context.nb,
            device_id,
            int(vm_id),
            bridge_name,
            context.tag_refs,
            context.now,
            overwrite_flags=context.overwrite_flags,
        )
        if vm_bridge_id:
            await _write_vm_interface_bridge_state(context, info, vm_bridge_id)
    except Exception as exc:
        logger.warning(
            "Failed to create bridge interfaces for %s on VM %s: %s",
            bridge_name,
            vm_id,
            exc,
        )


async def _reconcile_vm_interface_bridges(context: _VMInterfaceSyncContext) -> None:
    node_device_id_cache: dict[tuple[int | None, str, str], int | None] = {}
    for info in context.all_interface_info.values():
        await _reconcile_one_vm_interface_bridge(context, info, node_device_id_cache)


async def create_only_vm_interfaces(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_websocket: bool = False,
    use_guest_agent_interface_name: bool = True,
    ignore_ipv6_link_local_addresses: bool = True,
    primary_ip_preference: Literal["ipv4", "ipv6"] = "ipv4",
    overwrite_flags: SyncOverwriteFlags | None = None,
    sync_mac: bool = True,
    vm_interface_sync_strategy: VMInterfaceSyncStrategy = "guest_os_model",
) -> list[dict]:
    """Sync VM interfaces only (no VM creation) with per-interface progress events.

    Args:
        netbox_session: NetBox session.
        pxs: Proxmox sessions.
        cluster_status: Cluster status from Proxmox.
        cluster_resources: Filtered cluster resources containing VMs.
        tag: Proxbox tag reference.
        websocket: Optional bridge for SSE events.
        use_websocket: Whether to emit per-interface events.
        use_guest_agent_interface_name: Prefer guest-agent interface names.
        ignore_ipv6_link_local_addresses: Skip IPv6 link-local addresses.

    Returns:
        List of synced interface records.
    """
    nb = netbox_session
    tag_refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    tag_refs = [item for item in tag_refs if item.get("name") and item.get("slug")]
    normalized_strategy = normalize_vm_interface_sync_strategy(vm_interface_sync_strategy)
    if normalized_strategy == "legacy_rename":
        warn_legacy_vm_interface_strategy()

    vm_snapshot = await _load_netbox_virtual_machine_snapshot(nb)
    vm_snapshot = await hydrate_vm_identities_from_sidecars(
        nb,
        vm_snapshot,
        require_all=False,
        mode=SelectionMode.LENIENT,
    )
    sync_warnings = selection_skips(vm_snapshot)
    vm_snapshot = exclude_soft_deleted_vms(vm_snapshot, stage="VM interface")
    context = _VMInterfaceSyncContext(
        nb=nb,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        websocket=websocket,
        use_websocket=use_websocket,
        use_guest_agent_interface_name=use_guest_agent_interface_name,
        overwrite_flags=overwrite_flags,
        sync_mac=sync_mac,
        normalized_interface_strategy=normalized_strategy,
        tag_refs=tag_refs,
        now=datetime.now(timezone.utc),
        sync_warnings=sync_warnings,
        vm_index=_build_vm_index_by_proxmox_id(vm_snapshot),
        vm_candidates_by_vmid=_build_vm_candidates_by_proxmox_id(vm_snapshot),
        cluster_id_cache=_build_cluster_id_cache_from_vm_snapshot(vm_snapshot),
        endpoint_id_by_cluster=_endpoint_id_by_cluster_names(pxs, cluster_status),
        semaphore=asyncio.Semaphore(resolve_vm_sync_concurrency()),
    )

    await _collect_vm_interface_payloads(context)
    vlan_ids = await _reconcile_vm_interface_vlans(context)
    _apply_vm_interface_vlan_ids(context, vlan_ids)
    results = await _bulk_reconcile_vm_interfaces(context)
    await _reconcile_vm_interface_bridges(context)

    if use_websocket and websocket:
        await websocket.send_json({"object": "vm_interface", "end": True})

    return SyncResultList(results, warnings=sync_warnings)


async def create_only_vm_ip_addresses(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_websocket: bool = False,
    use_guest_agent_interface_name: bool = True,
    ignore_ipv6_link_local_addresses: bool = True,
    primary_ip_preference: Literal["ipv4", "ipv6"] = "ipv4",
    overwrite_flags: SyncOverwriteFlags | None = None,
    vm_interface_sync_strategy: VMInterfaceSyncStrategy = "guest_os_model",
) -> list[dict]:
    """Sync VM IP addresses and primary IP assignment.

    This function resolves the existing VM interfaces created by the interface
    sync stage, assigns the best discovered IPs to them, and promotes the first
    IP to primary IP when available.

    Args:
        netbox_session: NetBox session.
        pxs: Proxmox sessions.
        cluster_status: Cluster status from Proxmox.
        cluster_resources: Filtered cluster resources containing VMs.
        tag: Proxbox tag reference.
        websocket: Optional bridge for SSE events.
        use_websocket: Whether to emit per-IP events.
        use_guest_agent_interface_name: Prefer guest-agent interface names.
        ignore_ipv6_link_local_addresses: Skip IPv6 link-local addresses.

    Returns:
        List of synced IP address records.
    """
    from proxbox_api.services.sync.vm_network import set_primary_ip

    nb = netbox_session
    tag_refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    tag_refs = [t for t in tag_refs if t.get("name") and t.get("slug")]
    now = datetime.now(timezone.utc)
    results: list[dict] = []
    sync_warnings: list[dict[str, object]] = []
    normalized_interface_strategy = normalize_vm_interface_sync_strategy(vm_interface_sync_strategy)
    if normalized_interface_strategy == "legacy_rename":
        warn_legacy_vm_interface_strategy()

    vm_snapshot = await _load_netbox_virtual_machine_snapshot(nb)
    vm_snapshot = await hydrate_vm_identities_from_sidecars(
        nb,
        vm_snapshot,
        require_all=False,
        mode=SelectionMode.LENIENT,
    )
    sync_warnings.extend(selection_skips(vm_snapshot))
    vm_snapshot = exclude_soft_deleted_vms(vm_snapshot, stage="VM IP address")
    vm_index = _build_vm_index_by_proxmox_id(vm_snapshot)
    vm_candidates_by_vmid = _build_vm_candidates_by_proxmox_id(vm_snapshot)
    cluster_id_cache = _build_cluster_id_cache_from_vm_snapshot(vm_snapshot)
    endpoint_id_by_cluster = _endpoint_id_by_cluster_names(pxs, cluster_status)

    async def _sync_vm_ips(
        cluster_name: str,
        cluster_id: int | None,
        endpoint_id: int | None,
        resource: dict,
    ) -> tuple[list[dict], list[dict], dict]:  # noqa: C901
        """Collect IP payloads for a single VM. Returns (ip_payloads, first_ip_per_vm, ip_info)."""
        cluster_name_str = str(cluster_name)
        resource_node = str(resource.get("node", ""))
        vm_type = resource.get("type", "unknown")
        vm_name = str(resource.get("name", "")).strip()

        vmid = resource.get("vmid")
        if vmid is None:
            return [], [], {}

        netbox_vm = _resolve_vm_from_index_or_unique_vmid(
            vm_index,
            vm_candidates_by_vmid,
            endpoint_id=endpoint_id,
            raw_vmid=vmid,
            cluster_name=cluster_name_str,
            sync_context="IP address",
            cluster_id=cluster_id,
        )
        if not netbox_vm:
            logger.warning(
                "Skipping VM IP sync for %s (cluster=%s cluster_id=%s vmid=%s): "
                "NetBox VM not found",
                vm_name,
                cluster_name_str,
                cluster_id,
                vmid,
            )
            return [], [], {}

        proxmox_session = next(
            (
                px
                for px, cs in zip(pxs, cluster_status)
                if getattr(cs, "name", None) == cluster_name_str
            ),
            None,
        )

        vm_config: dict[str, object] = {}
        try:
            if proxmox_session and resource_node:
                vm_config_result = get_vm_config(
                    pxs=[proxmox_session],
                    node=resource_node,
                    type=vm_type,
                    vmid=int(vmid),
                )
                if inspect.isawaitable(vm_config_result):
                    vm_config_result = await vm_config_result
                vm_config = vm_config_result or {}
        except Exception as exc:
            logger.warning(
                "Could not fetch VM config for IP sync %s (vmid=%s): %s", vm_name, vmid, exc
            )

        guest_agent_interfaces: list[dict[str, object]] = []
        if vm_type == "qemu" and _parse_proxmox_kv_flag(vm_config.get("agent")):
            if proxmox_session and resource_node:
                guest_agent_interfaces = (
                    await get_qemu_guest_agent_network_interfaces(
                        proxmox_session, resource_node, int(vmid)
                    )
                    or []
                )

        guest_by_name = {
            str(iface.get("name", "")).strip().lower(): iface for iface in guest_agent_interfaces
        }
        guest_by_mac = build_guest_mac_index(guest_agent_interfaces)

        vm_dns_name = await _resolve_vm_dns_name(
            proxmox_session=proxmox_session,
            node=resource_node or None,
            vmid=vmid,
            vm_type=vm_type,
            vm_config=vm_config,
        )

        vm_networks = _parse_vm_networks(vm_config)
        ip_payloads: list[dict] = []
        first_ips: list[dict] = []  # Track first IP per VM
        ip_info: dict = {}
        missing_interface_count = 0  # NICs whose NetBox interface is not present

        # Pre-fetch interfaces for this VM to get their IDs
        from proxbox_api.netbox_rest import rest_list_async

        vm_interfaces = await rest_list_async(
            nb,
            "/api/virtualization/interfaces/",
            query={"virtual_machine_id": netbox_vm.get("id"), "limit": 500},
        )
        interface_name_to_id = {}
        for iface in vm_interfaces or []:
            raw_name = str(iface.get("name") or "").strip()
            normalized_name = normalize_vm_interface_name(raw_name)
            interface_name_to_id[normalized_name] = iface.get("id")

        for network in vm_networks:
            for iface_name, config_dict in network.items():
                config_interface_name = (
                    str(config_dict.get("name", iface_name)).strip() or iface_name
                )
                interface_mac = config_dict.get("virtio") or config_dict.get("hwaddr")
                guest_iface = None
                if interface_mac:
                    guest_iface = merged_guest_iface_from_mac_index(guest_by_mac, interface_mac)
                if guest_iface is None:
                    guest_iface = guest_by_name.get(config_interface_name.lower())

                resolved_name = config_interface_name
                if (
                    should_use_guest_agent_core_interface_name(
                        use_guest_agent_interface_name,
                        normalized_interface_strategy,
                    )
                    and guest_iface
                ):
                    guest_name = str(guest_iface.get("name") or "").strip()
                    if guest_name:
                        resolved_name = guest_name
                resolved_name = normalize_vm_interface_name(
                    resolved_name,
                    fallback=config_interface_name,
                    vm_name=vm_name,
                )
                interface_id = interface_name_to_id.get(resolved_name)
                if not interface_id:
                    # The IP stage can only attach an IP to a VM interface that
                    # already exists in NetBox.  Surface this at WARNING (not
                    # DEBUG) and count it so an IP-only run whose interfaces are
                    # stale/missing is diagnosable instead of silently
                    # reconciling nothing.  Run the VM-interface sync first.
                    missing_interface_count += 1
                    logger.warning(
                        "Skipping IP sync for interface %s on VM %s: interface "
                        "not found in NetBox (run the VM interface sync first)",
                        resolved_name,
                        vm_name,
                    )
                    continue

                if use_websocket and websocket:
                    await websocket.send_json(
                        {
                            "object": "vm_ip",
                            "data": {
                                "completed": False,
                                "sync_status": "syncing",
                                "rowid": resolved_name,
                                "name": resolved_name,
                                "vm": vm_name,
                            },
                        }
                    )

                try:
                    # Collect ALL IPs from guest agent (or fallback to config)
                    from proxbox_api.services.sync.network import build_vm_interface_ip_payload
                    from proxbox_api.services.sync.vm_helpers import all_guest_agent_ips

                    raw_guest_ip_count = 0
                    if isinstance(guest_iface, dict):
                        raw_guest_ip_count = sum(
                            1
                            for addr in (guest_iface.get("ip_addresses") or [])
                            if isinstance(addr, dict)
                        )

                    all_ips_for_iface: list[str] = []
                    if guest_iface:
                        all_ips_for_iface = all_guest_agent_ips(
                            guest_iface,
                            ignore_ipv6_link_local_addresses,
                            primary_ip_preference=primary_ip_preference,
                        )

                    skipped_guest_ips = max(0, raw_guest_ip_count - len(all_ips_for_iface))
                    if skipped_guest_ips and isinstance(websocket, WebSocketSSEBridge):
                        try:
                            await websocket.emit_phase_summary(
                                phase="vm-ip-addresses",
                                skipped=skipped_guest_ips,
                                message=(
                                    f"Skipped {skipped_guest_ips} link-local/zone-scoped/"
                                    f"loopback IPs on {vm_name}.{resolved_name}"
                                ),
                            )
                        except Exception as emit_exc:
                            logger.debug(
                                "emit_phase_summary failed for VM %s interface %s: %s",
                                vm_name,
                                resolved_name,
                                emit_exc,
                            )

                    if not all_ips_for_iface:
                        config_ip = config_dict.get("ip")
                        if config_ip and str(config_ip) != "dhcp":
                            all_ips_for_iface = [str(config_ip)]

                    all_ips_for_iface = preferred_primary_ip_order(
                        all_ips_for_iface,
                        primary_ip_preference=primary_ip_preference,
                    )

                    if all_ips_for_iface:
                        for interface_ip in all_ips_for_iface:
                            if interface_ip == "dhcp":
                                continue
                            payload = build_vm_interface_ip_payload(
                                interface_ip,
                                interface_id,
                                tag_refs,
                                now,
                                dns_name=vm_dns_name,
                                ignore_ipv6_link_local=ignore_ipv6_link_local_addresses,
                            )
                            if payload is None:
                                continue
                            ip_payloads.append(payload)

                            # Track first IP per address family for primary assignment.
                            # Both IPv4 and IPv6 are collected so set_primary_ip can
                            # designate each family independently.
                            addr_is_ipv6 = ":" in interface_ip
                            family_tracked = any(
                                (":" in e["address"]) == addr_is_ipv6 for e in first_ips
                            )
                            if not family_tracked:
                                first_ips.append(
                                    {
                                        "vm_id": netbox_vm.get("id"),
                                        "netbox_vm": netbox_vm,
                                        "address": interface_ip,
                                    }
                                )

                            ip_info[(payload["address"], int(interface_id))] = {
                                "address": payload["address"],
                                "interface_name": resolved_name,
                                "interface_id": interface_id,
                                "vm_name": vm_name,
                                "vm_id": netbox_vm.get("id"),
                                "mac_address": interface_mac,
                                "guest_interfaces": guest_agent_interfaces,
                            }
                    else:
                        if use_websocket and websocket:
                            await websocket.send_json(
                                {
                                    "object": "vm_ip",
                                    "data": {
                                        "completed": True,
                                        "rowid": resolved_name,
                                        "name": resolved_name,
                                        "vm": vm_name,
                                        "address": "No IP",
                                    },
                                }
                            )
                except Exception as exc:
                    logger.warning(
                        "Failed to collect IP payload for VM %s interface %s: %s",
                        vm_name,
                        resolved_name,
                        exc,
                    )
                    if use_websocket and websocket:
                        await websocket.send_json(
                            {
                                "object": "vm_ip",
                                "data": {
                                    "completed": False,
                                    "rowid": resolved_name,
                                    "name": resolved_name,
                                    "vm": vm_name,
                                    "error": str(exc),
                                },
                            }
                        )

        if missing_interface_count and isinstance(websocket, WebSocketSSEBridge):
            try:
                await websocket.emit_phase_summary(
                    phase="vm-ip-addresses",
                    skipped=missing_interface_count,
                    message=(
                        f"Skipped {missing_interface_count} IP(s) on {vm_name}: "
                        "their VM interface is not yet in NetBox — run the VM "
                        "interface sync before (or together with) IP addresses"
                    ),
                )
            except Exception as emit_exc:
                logger.debug(
                    "emit_phase_summary failed for missing interfaces on VM %s: %s",
                    vm_name,
                    emit_exc,
                )

        return ip_payloads, first_ips, ip_info

    max_concurrency = resolve_vm_sync_concurrency()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_task(
        cluster_name: str,
        cluster_id: int | None,
        endpoint_id: int | None,
        resource: dict,
    ) -> tuple[list[dict], list[dict], dict]:
        async with semaphore:
            return await _sync_vm_ips(cluster_name, cluster_id, endpoint_id, resource)

    async def _create_cluster_tasks(cluster: dict) -> list:
        tasks = []
        for cluster_name, resources in cluster.items():
            cluster_id = await resolve_netbox_cluster_id_by_name(
                nb,
                str(cluster_name),
                cache=cluster_id_cache,
            )
            endpoint_id = endpoint_id_by_cluster.get(str(cluster_name))
            for resource in resources:
                if resource.get("type") in ("qemu", "lxc"):
                    tasks.append(_run_task(cluster_name, cluster_id, endpoint_id, resource))
        return await asyncio.gather(*tasks, return_exceptions=True)

    # Collect all IP payloads and metadata from all VMs
    all_ip_payloads: list[dict] = []
    all_ip_info: dict = {}
    vms_with_first_ips: list[dict] = []

    try:
        for cluster in cluster_resources:
            cluster_results = await _create_cluster_tasks(cluster)
            for cluster_result in cluster_results:
                if isinstance(cluster_result, Exception):
                    continue
                ip_payloads, first_ips, ip_info = cluster_result
                if isinstance(ip_payloads, list):
                    all_ip_payloads.extend(ip_payloads)
                    all_ip_info.update(ip_info)
                    vms_with_first_ips.extend(first_ips)
    except Exception as exc:
        error_detail = getattr(exc, "detail", str(exc))
        error_msg = f"{type(exc).__name__}: {error_detail}"
        logger.warning("Error during VM IP address collection: %s", error_msg)

    # Bulk reconcile IP addresses
    if all_ip_payloads:
        try:
            from proxbox_api.services.sync.network import bulk_reconcile_vm_interface_ips

            created_ips = await bulk_reconcile_vm_interface_ips(
                nb, all_ip_payloads, overwrite_flags=overwrite_flags
            )
            logger.info(
                "Bulk IP address reconciliation completed: %d IPs processed",
                len(all_ip_payloads),
            )

            # Emit WebSocket progress for each created IP
            if use_websocket and websocket:
                for ip_record in created_ips:
                    address = ip_record.get("address")
                    assigned_interface_id = _relation_id(ip_record.get("assigned_object_id"))
                    scoped_key = (
                        (str(address), int(assigned_interface_id))
                        if address and assigned_interface_id is not None
                        else None
                    )
                    info = all_ip_info.get(scoped_key) if scoped_key else None
                    if info is None and address:
                        address_matches = [
                            candidate
                            for (candidate_address, _iface_id), candidate in all_ip_info.items()
                            if candidate_address == address
                        ]
                        if len(address_matches) == 1:
                            info = address_matches[0]
                    if info is not None:
                        await websocket.send_json(
                            {
                                "object": "vm_ip",
                                "data": {
                                    "completed": True,
                                    "rowid": info.get("interface_name"),
                                    "name": info.get("interface_name"),
                                    "vm": info.get("vm_name"),
                                    "ip_id": ip_record.get("id"),
                                    "address": address,
                                },
                            }
                        )

            # Build results list for compatibility
            results = [
                {
                    "ip_id": ip.get("id"),
                    "address": ip.get("address"),
                }
                for ip in created_ips
            ]

            guest_contexts: dict[int, dict[str, object]] = {}
            for ip_record in created_ips:
                if not isinstance(ip_record, dict):
                    serialize = getattr(ip_record, "serialize", None)
                    ip_record = serialize() if callable(serialize) else {}
                address = ip_record.get("address") if isinstance(ip_record, dict) else None
                ip_id = ip_record.get("id") if isinstance(ip_record, dict) else None
                if not address or ip_id is None:
                    continue
                assigned_interface_id = (
                    _relation_id(ip_record.get("assigned_object_id"))
                    if isinstance(ip_record, dict)
                    else None
                )
                scoped_key = (
                    (str(address), int(assigned_interface_id))
                    if assigned_interface_id is not None
                    else None
                )
                info = all_ip_info.get(scoped_key) if scoped_key else None
                if info is None:
                    address_matches = [
                        candidate
                        for (candidate_address, _iface_id), candidate in all_ip_info.items()
                        if candidate_address == address
                    ]
                    if len(address_matches) == 1:
                        info = address_matches[0]
                if not info:
                    continue
                vm_id_raw = info.get("vm_id")
                interface_id_raw = info.get("interface_id")
                try:
                    vm_id_int = int(vm_id_raw)
                    interface_id_int = int(interface_id_raw)
                    ip_id_int = int(ip_id)
                except (TypeError, ValueError):
                    continue
                context = guest_contexts.setdefault(
                    vm_id_int,
                    {
                        "guest_interfaces": info.get("guest_interfaces") or [],
                        "core_interface_id_by_mac": {},
                        "ip_ids_by_interface_id": {},
                    },
                )
                if not context.get("guest_interfaces"):
                    context["guest_interfaces"] = info.get("guest_interfaces") or []
                mac_value = info.get("mac_address")
                if mac_value:
                    cast("dict[str, int]", context["core_interface_id_by_mac"])[str(mac_value)] = (
                        interface_id_int
                    )
                ip_ids_by_interface = cast(
                    "dict[int, dict[str, int]]",
                    context["ip_ids_by_interface_id"],
                )
                ip_ids_by_interface.setdefault(interface_id_int, {})[str(address)] = ip_id_int

            for vm_id_int, context in guest_contexts.items():
                await reconcile_guest_vm_interfaces(
                    nb,
                    vm_id_int,
                    cast("list[dict[str, object]]", context.get("guest_interfaces") or []),
                    cast("dict[str, int]", context.get("core_interface_id_by_mac") or {}),
                    cast(
                        "dict[int, dict[str, int]]",
                        context.get("ip_ids_by_interface_id") or {},
                    ),
                    tag_refs,
                    normalized_interface_strategy,
                )

            # Cleanup stale IPs per interface: remove any Proxbox-tagged IPs on the
            # interface that were NOT in this sync run
            from proxbox_api.services.sync.network import cleanup_stale_ips_for_interface

            interface_current_ips: dict[int, set[str]] = {}
            for payload in all_ip_payloads:
                iface_id = payload.get("assigned_object_id")
                address = payload.get("address")
                if iface_id and address:
                    interface_current_ips.setdefault(int(iface_id), set()).add(str(address))

            for iface_id, current_set in interface_current_ips.items():
                try:
                    await cleanup_stale_ips_for_interface(nb, iface_id, current_set)
                except Exception as cleanup_exc:
                    logger.warning(
                        "Failed to cleanup stale IPs for interface id=%s: %s",
                        iface_id,
                        cleanup_exc,
                    )

        except Exception as e:
            logger.error("Error during bulk IP reconciliation: %s", e)
    else:
        results = []

    # Set primary IPs per VM (low volume, keep as per-VM operations)
    if vms_with_first_ips:
        try:
            for vm_info in vms_with_first_ips:
                netbox_vm = vm_info.get("netbox_vm")
                if netbox_vm:
                    # Fetch the IP record to get its ID for primary assignment
                    from proxbox_api.netbox_rest import rest_first_async

                    ip_record = await rest_first_async(
                        nb,
                        "/api/ipam/ip-addresses/",
                        query={"address": vm_info.get("address"), "limit": 1},
                    )
                    if ip_record:
                        ip_id = (
                            ip_record.get("id")
                            if isinstance(ip_record, dict)
                            else getattr(ip_record, "id", None)
                        )
                        if ip_id:
                            await set_primary_ip(
                                nb=nb,
                                virtual_machine=netbox_vm,
                                primary_ip_id=ip_id,
                                primary_ip_preference=primary_ip_preference,
                            )
        except Exception as e:
            logger.warning("Error setting primary IPs: %s", e)

    if use_websocket and websocket:
        await websocket.send_json({"object": "vm_ip", "end": True})

    return attach_skips_to_list(results, sync_warnings)


@router.get(
    "/{netbox_vm_id}/create",
    dependencies=[
        Depends(ensure_netbox_sync_dependencies),
        Depends(reset_sidecar_availability_cache),
    ],
)
async def create_virtual_machine_by_netbox_id(
    netbox_vm_id: int,
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "Compatibility toggle for legacy_rename. In guest_os_model, core "
            "VMInterfaces keep Proxmox netX names and guest OS names are written "
            "to plugin guest interface rows."
        ),
    ),
    vm_interface_sync_strategy: Literal["guest_os_model", "legacy_rename"] = Query(
        default="guest_os_model",
        title="VM Interface Sync Strategy",
        description=(
            "guest_os_model keeps core VMInterfaces named from Proxmox config (netX) "
            "and writes guest OS interfaces to the netbox-proxbox plugin. "
            "legacy_rename preserves the deprecated guest-agent rename behavior."
        ),
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=(
            "When true, IPv6 link-local addresses (fe80::/64) are ignored during "
            "VM interface IP address selection. Disable only if you need link-local addresses included."
        ),
    ),
    primary_ip_preference: Literal["ipv4", "ipv6"] = Query(
        default="ipv4",
        title="Primary IP Preference",
        description="Preferred IP family when choosing VM primary IP (ipv4 or ipv6).",
    ),
    sync_task_history: bool = Query(
        default=True,
        title="Sync Task History",
        description=(
            "Synchronize archived Proxmox task history for the targeted VM. "
            "Disable when a dedicated task-history stage follows immediately."
        ),
    ),
    run_id: str | None = Query(
        default=None,
        title="Run ID",
        description=(
            "UUID stored in each touched VM's typed sync-state sidecar. "
            "When omitted, a fresh UUID is generated."
        ),
    ),
    overwrite_flags: ResolvedSyncOverwriteFlagsDep = SyncOverwriteFlags(),
):
    return await _create_virtual_machine_by_netbox_id(
        netbox_vm_id=netbox_vm_id,
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        tag=tag,
        use_guest_agent_interface_name=use_guest_agent_interface_name,
        vm_interface_sync_strategy=vm_interface_sync_strategy,
        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
        primary_ip_preference=primary_ip_preference,
        sync_task_history=sync_task_history,
        overwrite_flags=overwrite_flags,
        run_id=run_id,
    )


@router.get(
    "/create/stream",
    response_model=None,
    dependencies=[
        Depends(ensure_netbox_sync_dependencies),
        Depends(reset_sidecar_availability_cache),
    ],
)
async def create_virtual_machines_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "Compatibility toggle for legacy_rename. In guest_os_model, core "
            "VMInterfaces keep Proxmox netX names and guest OS names are written "
            "to plugin guest interface rows."
        ),
    ),
    vm_interface_sync_strategy: Literal["guest_os_model", "legacy_rename"] = Query(
        default="guest_os_model",
        title="VM Interface Sync Strategy",
        description=(
            "guest_os_model keeps core VMInterfaces named from Proxmox config (netX) "
            "and writes guest OS interfaces to the netbox-proxbox plugin. "
            "legacy_rename preserves the deprecated guest-agent rename behavior."
        ),
    ),
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description="Comma-separated list of NetBox VM IDs to sync. When provided, only these VMs will be synced.",
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=(
            "When true, IPv6 link-local addresses (fe80::/64) are ignored during "
            "VM interface IP address selection. Disable only if you need link-local addresses included."
        ),
    ),
    primary_ip_preference: Literal["ipv4", "ipv6"] = Query(
        default="ipv4",
        title="Primary IP Preference",
        description="Preferred IP family when choosing VM primary IP (ipv4 or ipv6).",
    ),
    overwrite_vm_role: bool | None = Query(
        default=None,
        title="Overwrite VM Role",
        description=(
            "When false, the VM role is not patched on existing VMs that already have a role. "
            "The role is still set when a VM is first created. "
            "When unset, falls back to overwrite_flags.overwrite_vm_role."
        ),
    ),
    overwrite_vm_type: bool | None = Query(
        default=None,
        title="Overwrite VM Type",
        description=(
            "When false, the VM type is not patched on existing VMs that already have a type. "
            "The type is still set when a VM is first created. "
            "When unset, falls back to overwrite_flags.overwrite_vm_type."
        ),
    ),
    overwrite_vm_tags: bool | None = Query(
        default=None,
        title="Overwrite VM Tags",
        description=(
            "When false, tags are not patched on existing VMs that already have tags. "
            "Tags are still applied when a VM is first created. "
            "When unset, falls back to overwrite_flags.overwrite_vm_tags."
        ),
    ),
    overwrite_vm_description: bool | None = Query(
        default=None,
        title="Overwrite VM Description",
        description=(
            "When false, the VM description is not patched on existing VMs that already "
            "have a non-empty description. The description is still set on first create. "
            "When unset, falls back to overwrite_flags.overwrite_vm_description."
        ),
    ),
    overwrite_vm_custom_fields: bool | None = Query(
        default=None,
        title="Overwrite VM Sync State",
        description=(
            "Compatibility-named flag controlling typed VM sync-state sidecar updates. "
            "It no longer writes NetBox custom fields. "
            "When unset, falls back to overwrite_flags.overwrite_vm_custom_fields."
        ),
    ),
    sync_vm_network: bool = Query(
        default=True,
        title="Sync VM Network",
        description=(
            "When false, VM interface and IP address reconciliation is skipped in this pass. "
            "Use when a dedicated network-sync stage follows immediately after."
        ),
    ),
    sync_task_history: bool = Query(
        default=True,
        title="Sync Task History",
        description=(
            "Synchronize archived Proxmox task history after successful VM reconciliation. "
            "Disable when a dedicated task-history stage follows immediately."
        ),
    ),
    run_id: str | None = Query(
        default=None,
        title="Run ID",
        description=(
            "UUID stored in each touched VM's typed sync-state sidecar. "
            "When omitted, a fresh UUID is generated."
        ),
    ),
    assign_vm_interface_ips: bool = Query(
        default=True,
        title="Assign VM Interface IPs",
        description=(
            "When false, IP address reconciliation is skipped for VM interfaces in this pass. "
            "The VMInterface and MAC address are still created or updated."
        ),
    ),
    sync_vm_interface_macs: bool = Query(
        default=True,
        title="Sync VM Interface MACs",
        description=(
            "When false, MAC address reconciliation is skipped for VM interfaces in this pass. "
            "The VMInterface and IP addresses are still created or updated."
        ),
    ),
    sync_mode_vm: str = Query(
        default="always",
        title="Sync Mode (VM)",
        description=(
            "Controls whether non-template VMs are included in this sync pass. "
            "'always' (default): sync all non-template VMs. "
            "'bootstrap_only': treated as enabled at the backend. "
            "'disabled': non-template VMs are skipped entirely in this pass. "
            "Unknown values fall back to 'always' with a warning."
        ),
    ),
    sync_mode_vm_template: str = Query(
        default="always",
        title="Sync Mode (VM Template)",
        description=(
            "Controls whether template VMs are included in this sync pass. "
            "'always' (default): sync all templates. "
            "'bootstrap_only': treated as enabled at the backend. "
            "'disabled': template VMs are skipped entirely in this pass. "
            "Unknown values fall back to 'always' with a warning."
        ),
    ),
    overwrite_flags: ResolvedSyncOverwriteFlagsDep = SyncOverwriteFlags(),
):
    (
        overwrite_vm_role,
        overwrite_vm_type,
        overwrite_vm_tags,
        overwrite_vm_description,
        overwrite_vm_custom_fields,
    ) = _resolve_vm_overwrites(
        overwrite_vm_role,
        overwrite_vm_type,
        overwrite_vm_tags,
        overwrite_vm_description,
        overwrite_vm_custom_fields,
        overwrite_flags,
    )

    filtered_cluster_resources = cluster_resources
    try:
        selected_vm_ids = parse_selected_netbox_vm_ids(netbox_vm_ids)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    vm_ids = selected_vm_ids or []

    if selected_vm_ids is not None:
        filtered_cluster_resources = await _filter_cluster_resources_by_netbox_vm_ids(
            netbox_session=netbox_session,
            cluster_resources=cluster_resources,
            netbox_vm_ids=selected_vm_ids,
            pxs=pxs,
            cluster_status=cluster_status,
        )
    # VMs the lenient selection dropped, reported on the final stage result.
    selection_skipped = selection_skips(filtered_cluster_resources)

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await create_virtual_machines(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=filtered_cluster_resources,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                    use_guest_agent_interface_name=use_guest_agent_interface_name,
                    vm_interface_sync_strategy=vm_interface_sync_strategy,
                    ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
                    primary_ip_preference=primary_ip_preference,
                    overwrite_vm_role=overwrite_vm_role,
                    overwrite_vm_type=overwrite_vm_type,
                    overwrite_vm_tags=overwrite_vm_tags,
                    overwrite_vm_description=overwrite_vm_description,
                    overwrite_vm_custom_fields=overwrite_vm_custom_fields,
                    sync_vm_network=sync_vm_network,
                    sync_task_history=sync_task_history,
                    overwrite_flags=overwrite_flags,
                    run_id=run_id,
                    assign_vm_interface_ips=assign_vm_interface_ips,
                    sync_vm_interface_macs=sync_vm_interface_macs,
                    sync_mode_vm=sync_mode_vm,
                    sync_mode_vm_template=sync_mode_vm_template,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "started",
                    "message": "Starting virtual machines synchronization."
                    if not vm_ids
                    else f"Starting virtual machines synchronization for {len(vm_ids)} VM(s).",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame

            result = await sync_task
            result_payload = _stream_stage_payload(result, selection_skipped)
            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "completed",
                    "message": "Virtual machines synchronization finished.",
                    "result": result_payload,
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Virtual machines sync completed.",
                    "result": result_payload,
                },
            )
        except asyncio.CancelledError:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            yield sse_event(
                "error",
                {
                    "step": "virtual-machines",
                    "status": "failed",
                    "error": "Server shutdown or request cancelled.",
                    "detail": "Server shutdown or request cancelled.",
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual machines sync cancelled.",
                    "errors": [{"detail": "Server shutdown or request cancelled."}],
                },
            )
        except Exception as error:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            yield sse_event(
                "error",
                {
                    "step": "virtual-machines",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual machines sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )
        finally:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await asyncio.shield(sync_task)
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/{netbox_vm_id}/create/stream",
    response_model=None,
    dependencies=[
        Depends(ensure_netbox_sync_dependencies),
        Depends(reset_sidecar_availability_cache),
    ],
)
async def create_virtual_machine_by_netbox_id_stream(
    netbox_vm_id: int,
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "Compatibility toggle for legacy_rename. In guest_os_model, core "
            "VMInterfaces keep Proxmox netX names and guest OS names are written "
            "to plugin guest interface rows."
        ),
    ),
    vm_interface_sync_strategy: Literal["guest_os_model", "legacy_rename"] = Query(
        default="guest_os_model",
        title="VM Interface Sync Strategy",
        description=(
            "guest_os_model keeps core VMInterfaces named from Proxmox config (netX) "
            "and writes guest OS interfaces to the netbox-proxbox plugin. "
            "legacy_rename preserves the deprecated guest-agent rename behavior."
        ),
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=(
            "When true, IPv6 link-local addresses (fe80::/64) are ignored during "
            "VM interface IP address selection. Disable only if you need link-local addresses included."
        ),
    ),
    primary_ip_preference: Literal["ipv4", "ipv6"] = Query(
        default="ipv4",
        title="Primary IP Preference",
        description="Preferred IP family when choosing VM primary IP (ipv4 or ipv6).",
    ),
    sync_task_history: bool = Query(
        default=True,
        title="Sync Task History",
        description=(
            "Synchronize archived Proxmox task history for the targeted VM. "
            "Disable when a dedicated task-history stage follows immediately."
        ),
    ),
    overwrite_vm_role: bool | None = Query(
        default=None,
        title="Overwrite VM Role",
        description=(
            "When false, the VM role is not patched on existing VMs that already have a role. "
            "The role is still set when a VM is first created. "
            "When unset, falls back to overwrite_flags.overwrite_vm_role."
        ),
    ),
    overwrite_vm_type: bool | None = Query(
        default=None,
        title="Overwrite VM Type",
        description=(
            "When false, the VM type is not patched on existing VMs that already have a type. "
            "The type is still set when a VM is first created. "
            "When unset, falls back to overwrite_flags.overwrite_vm_type."
        ),
    ),
    overwrite_vm_tags: bool | None = Query(
        default=None,
        title="Overwrite VM Tags",
        description=(
            "When false, tags are not patched on existing VMs that already have tags. "
            "Tags are still applied when a VM is first created. "
            "When unset, falls back to overwrite_flags.overwrite_vm_tags."
        ),
    ),
    overwrite_vm_description: bool | None = Query(
        default=None,
        title="Overwrite VM Description",
        description=(
            "When false, the VM description is not patched on existing VMs that already "
            "have a non-empty description. The description is still set on first create. "
            "When unset, falls back to overwrite_flags.overwrite_vm_description."
        ),
    ),
    overwrite_vm_custom_fields: bool | None = Query(
        default=None,
        title="Overwrite VM Sync State",
        description=(
            "Compatibility-named flag controlling typed VM sync-state sidecar updates. "
            "It no longer writes NetBox custom fields. "
            "When unset, falls back to overwrite_flags.overwrite_vm_custom_fields."
        ),
    ),
    run_id: str | None = Query(
        default=None,
        title="Run ID",
        description=(
            "UUID stored in each touched VM's typed sync-state sidecar. "
            "When omitted, a fresh UUID is generated."
        ),
    ),
    overwrite_flags: ResolvedSyncOverwriteFlagsDep = SyncOverwriteFlags(),
):
    (
        overwrite_vm_role,
        overwrite_vm_type,
        overwrite_vm_tags,
        overwrite_vm_description,
        overwrite_vm_custom_fields,
    ) = _resolve_vm_overwrites(
        overwrite_vm_role,
        overwrite_vm_type,
        overwrite_vm_tags,
        overwrite_vm_description,
        overwrite_vm_custom_fields,
        overwrite_flags,
    )

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await _create_virtual_machine_by_netbox_id(
                    netbox_vm_id=netbox_vm_id,
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                    use_guest_agent_interface_name=use_guest_agent_interface_name,
                    vm_interface_sync_strategy=vm_interface_sync_strategy,
                    ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
                    primary_ip_preference=primary_ip_preference,
                    sync_task_history=sync_task_history,
                    overwrite_vm_role=overwrite_vm_role,
                    overwrite_vm_type=overwrite_vm_type,
                    overwrite_vm_tags=overwrite_vm_tags,
                    overwrite_vm_description=overwrite_vm_description,
                    overwrite_vm_custom_fields=overwrite_vm_custom_fields,
                    overwrite_flags=overwrite_flags,
                    run_id=run_id,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "virtual-machine",
                    "status": "started",
                    "message": f"Starting virtual machine synchronization for id={netbox_vm_id}.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame

            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "virtual-machine",
                    "status": "completed",
                    "message": "Virtual machine synchronization finished.",
                    "result": {"count": len(result)},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Virtual machine sync completed.",
                    "result": {"count": len(result)},
                },
            )
        except asyncio.CancelledError:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            yield sse_event(
                "error",
                {
                    "step": "virtual-machine",
                    "status": "failed",
                    "error": "Server shutdown or request cancelled.",
                    "detail": "Server shutdown or request cancelled.",
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual machine sync cancelled.",
                    "errors": [{"detail": "Server shutdown or request cancelled."}],
                },
            )
        except HTTPException as error:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            yield sse_event(
                "error",
                {
                    "step": "virtual-machine",
                    "status": "failed",
                    "error": str(error.detail),
                    "detail": str(error.detail),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual machine sync failed.",
                    "errors": [{"detail": str(error.detail)}],
                },
            )
        except Exception as error:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            yield sse_event(
                "error",
                {
                    "step": "virtual-machine",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual machine sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )
        finally:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await asyncio.shield(sync_task)
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
