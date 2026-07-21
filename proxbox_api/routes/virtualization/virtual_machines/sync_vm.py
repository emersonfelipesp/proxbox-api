"""Virtual machine creation sync and SSE stream endpoints."""

# FastAPI Imports
import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast

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
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_compat import VirtualMachine
from proxbox_api.netbox_rest import (
    clear_rest_get_cache_for_path,
    rest_create_async,
    rest_first_async,
    rest_list_async,
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
    _parse_proxmox_kv_flag,
)
from proxbox_api.routes.extras import CreateCustomFieldsDep
from proxbox_api.routes.proxmox import get_vm_config
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.routes.virtualization.virtual_machines.helpers import (
    resolve_netbox_write_concurrency,
    resolve_vm_sync_concurrency,
)
from proxbox_api.schemas.stream_messages import ErrorCategory, ItemOperation, SubstepStatus
from proxbox_api.schemas.sync import SyncBehaviorFlags, SyncOverwriteFlags
from proxbox_api.services.custom_fields import (
    include_custom_fields_in_payload,
    legacy_custom_field_fallback_query,
    legacy_custom_fields_payload,
)
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
from proxbox_api.services.sync.storage_links import (
    build_storage_index,
    find_storage_record,
    storage_name_from_volume_id,
)
from proxbox_api.services.sync.sync_state_reader import (
    load_vm_last_synced_names,
    resolve_virtual_machine_by_sync_state,
)
from proxbox_api.services.sync.sync_state_writer import (
    reset_sidecar_availability_cache,
    write_virtual_disk_sync_state,
    write_virtual_machine_sync_state,
    write_vm_interface_sync_state,
)
from proxbox_api.services.sync.tag_resolver import resolve_proxmox_tag_ids
from proxbox_api.services.sync.task_history import (
    sync_virtual_machine_task_history,
)
from proxbox_api.services.sync.virtual_machines import (
    build_netbox_virtual_machine_payload,
)
from proxbox_api.services.sync.vm_create import ensure_vm_type
from proxbox_api.services.sync.vm_helpers import (
    _compute_vm_patchable_fields,
    build_guest_mac_index,
    extract_vm_disk_aggregate_size,
    merged_guest_iface_from_mac_index,
    parse_comma_separated_ints,
    parse_proxmox_net_configs,
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
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils import return_status_html
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

router = APIRouter()


class SyncResultList(list[dict]):
    """List result with optional sync warnings for callers that can surface them."""

    def __init__(
        self,
        values: list[dict] | None = None,
        *,
        warnings: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(values or [])
        self.warnings = warnings or []


@dataclass(slots=True)
class _VMPreparationContext:
    """Run-scoped dependencies for preparing a VM from a fetched Proxmox config."""

    nb: object
    tag: object
    overwrite_flags: SyncOverwriteFlags
    behavior_flags: SyncBehaviorFlags
    effective_vm_overwrite_flags: SyncOverwriteFlags
    cluster_dependency_cache: dict[str, dict[str, object]]
    node_device_cache: dict[tuple[str, str], object]
    vm_role_cache: dict[str, object]
    vm_role_mapping: dict[str, dict[str, object]]
    tag_refs: list[dict[str, object]]
    proxmox_url_by_cluster: dict[str, str]
    endpoint_id_by_cluster: dict[str, int]
    resolve_vm_type: Callable[[str], Awaitable[object | None]]
    resolve_vm_proxmox_tag_ids: Callable[[str, dict[str, object]], Awaitable[list[int]]]


def _vm_identity_lookup(
    *,
    vmid: object,
    endpoint_id: int | None,
    cluster_id: int | None,
) -> dict[str, object]:
    """Build the safest available NetBox lookup for a Proxmox VM."""
    lookup: dict[str, object] = {"cf_proxmox_vm_id": int(vmid)}
    if endpoint_id is not None:
        lookup["cf_proxmox_endpoint_id"] = endpoint_id
    elif cluster_id is not None:
        lookup["cluster_id"] = cluster_id
    return lookup


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
) -> _PreparedVMState:
    """Prepare desired NetBox VM state from an already fetched Proxmox config."""

    vm_type = str(resource.get("type") or "unknown")
    vm_type_key = vm_type.lower() if vm_type else "undefined"
    if vm_type_key not in context.vm_role_mapping:
        vm_type_key = "undefined"

    vm_config_obj = await asyncio.to_thread(ProxmoxVmConfigInput.model_validate, vm_config)

    cluster_dependencies = context.cluster_dependency_cache.get(str(cluster_name), {})
    cluster = cluster_dependencies.get("cluster")
    if cluster is None:
        raise ProxboxException(
            message="Error creating Virtual Machine dependent objects (cluster, device, tag and role)",
            python_exception=f"Missing precomputed cluster dependency for cluster={cluster_name}",
        )

    node_name = str(resource.get("node"))
    site_id = _cluster_dependency_site_id(cluster_dependencies)
    device = context.node_device_cache.get((str(cluster_name), node_name))
    if device is None:
        device = await _ensure_device(
            context.nb,
            device_name=node_name,
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
        context.node_device_cache[(str(cluster_name), node_name)] = device

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
    desired_payload = await asyncio.to_thread(
        build_netbox_virtual_machine_payload,
        proxmox_resource=resource,
        proxmox_config=vm_config,
        cluster_id=int(getattr(cluster, "id", 0) or 0),
        device_id=int(getattr(device, "id", 0) or 0),
        role_id=None if vm_type_id else int(getattr(role, "id", 0) or 0),
        tag_ids=merged_tag_ids,
        site_id=site_id,
        tenant_id=int(getattr(cluster_dependencies.get("tenant"), "id", 0) or 0) or None,
        virtual_machine_type_id=vm_type_id,
        last_updated=now,
        cluster_name=str(cluster_name),
        proxmox_url=context.proxmox_url_by_cluster.get(str(cluster_name)),
        endpoint_id=context.endpoint_id_by_cluster.get(str(cluster_name)),
        parse_description_metadata=context.behavior_flags.parse_description_metadata,
        overwrite_flags=context.effective_vm_overwrite_flags,
    )
    cluster_id = int(getattr(cluster, "id", 0) or 0) or None
    endpoint_id = context.endpoint_id_by_cluster.get(str(cluster_name))
    lookup = _vm_identity_lookup(
        vmid=resource.get("vmid"),
        endpoint_id=endpoint_id,
        cluster_id=cluster_id,
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

    page_size = 200
    offset = 0
    snapshot: list[dict[str, object]] = []

    while True:
        records = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"limit": page_size, "offset": offset},
        )
        if not records:
            break

        serialized_page: list[dict[str, object]] = []
        for record in records:
            serialized = _to_mapping(record)
            if serialized:
                serialized_page.append(serialized)

        snapshot.extend(serialized_page)
        if len(records) < page_size:
            break
        offset += page_size

    return snapshot


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
    custom_fields = record.get("custom_fields")
    if isinstance(custom_fields, dict):
        merged_custom_fields = dict(custom_fields)
    else:
        merged_custom_fields = {}

    merged_custom_fields["proxmox_vm_id"] = proxmox_vmid
    if endpoint_id is not None:
        merged_custom_fields["proxmox_endpoint_id"] = endpoint_id
    vm_type = str(prepared.vm_type or "").strip().lower()
    if vm_type in {"qemu", "lxc"}:
        merged_custom_fields["proxmox_vm_type"] = vm_type
    record["custom_fields"] = merged_custom_fields


async def _hydrate_vm_snapshot_with_sidecar_identity(
    nb: object,
    *,
    prepared_vms: list[_PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
    custom_fields_enabled_flag: bool | None = None,
) -> int:
    """Overlay sidecar VM identity onto snapshot records that lack legacy CFs.

    The reconciliation queue is intentionally pure and indexes the loaded VM
    snapshot. Before building that queue, use the sidecar resolver for prepared
    VMs that are not already owned by legacy custom fields so sidecar-only rows
    are adopted instead of treated as name collisions or creates.
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
    resolved_keys: set[tuple[int | None, int, str]] = set()
    hydrated = 0

    for prepared in prepared_vms:
        proxmox_vmid = _prepared_proxmox_vmid(prepared)
        if proxmox_vmid is None:
            continue
        endpoint_id = extract_proxmox_endpoint_id(prepared.desired_payload)
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
        resolver_key = (endpoint_id, proxmox_vmid, vm_type)
        if resolver_key in resolved_keys:
            continue
        resolved_keys.add(resolver_key)

        resolution = await resolve_virtual_machine_by_sync_state(
            nb,
            proxmox_vm_id=proxmox_vmid,
            endpoint_id=endpoint_id,
            cluster_id=cluster_id,
            fallback_query=legacy_custom_field_fallback_query(
                prepared.lookup,
                enabled=custom_fields_enabled_flag,
            ),
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
    """Extract a positive ``proxmox_vm_id`` custom field from a VM snapshot record."""
    custom_fields = vm.get("custom_fields")
    if not isinstance(custom_fields, dict):
        return None
    try:
        vmid = int(str(custom_fields.get("proxmox_vm_id") or "").strip())
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


def _resolve_vm_from_index_or_unique_vmid(
    vm_index: dict[tuple[int, int], dict[str, object]],
    candidates_by_vmid: dict[int, list[dict[str, object]]],
    *,
    endpoint_id: int | None,
    raw_vmid: object,
    cluster_name: str,
    sync_context: str,
) -> dict[str, object] | None:
    """Resolve a VM by endpoint-scoped key, with legacy fallback only when safe."""
    try:
        vmid = int(str(raw_vmid).strip())
    except (TypeError, ValueError):
        return None

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
    custom_fields: bool | None,
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
        custom_fields if custom_fields is not None else overwrite_flags.overwrite_vm_custom_fields,
    )


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

    (
        endpoint_typed_vm_index,
        endpoint_untyped_vm_candidates,
        cluster_typed_vm_index,
        cluster_untyped_vm_candidates,
    ) = _build_vm_snapshot_identity_indexes(netbox_snapshot)
    # One fetch for the whole pass. The resolver needs the last-synced Proxmox
    # name for every VM it examines; looking it up per VM would add an N+1 REST
    # round trip across the fleet. Empty when the sidecar API is unavailable or
    # nothing has been re-synced yet, which the resolver treats as "no evidence".
    last_synced_names: dict[int, str] = {}
    if nb is not None:
        last_synced_names = await load_vm_last_synced_names(nb)

    cluster_used_names: dict[int, set[str]] = {}
    cluster_no_id_used_names: set[str] = set()
    for record in netbox_snapshot:
        cluster_id = _relation_id(record.get("cluster"))
        name = record.get("name")
        if not isinstance(name, str) or not name:
            continue
        if cluster_id is None:
            continue
        cluster_used_names.setdefault(cluster_id, set()).add(name)

    grouped: dict[int | None, list[_PreparedVMState]] = {}
    for prepared in prepared_vms:
        cluster_id = _relation_id(prepared.desired_payload.get("cluster"))
        grouped.setdefault(cluster_id, []).append(prepared)

    def _sort_key(p: _PreparedVMState) -> tuple[str, int]:
        try:
            vmid = int(p.resource.get("vmid", 0) or 0)
        except (TypeError, ValueError):
            vmid = 0
        return (p.cluster_name.casefold(), vmid)

    resolutions: list[NameResolution] = []

    for cluster_id, group in grouped.items():
        if cluster_id is None:
            used = cluster_no_id_used_names
        else:
            used = cluster_used_names.setdefault(cluster_id, set())

        # Strip names owned by the VMIDs we're about to write — they'll be
        # re-registered (possibly under a different name) by the resolver.
        for prepared in group:
            try:
                vmid = int(prepared.resource.get("vmid", 0) or 0)
            except (TypeError, ValueError):
                vmid = 0
            existing = _select_existing_vm_record(
                prepared=prepared,
                endpoint_id=extract_proxmox_endpoint_id(prepared.desired_payload),
                cluster_id=cluster_id,
                proxmox_vmid=vmid or None,
                endpoint_typed_index=endpoint_typed_vm_index,
                endpoint_untyped_candidates=endpoint_untyped_vm_candidates,
                cluster_typed_index=cluster_typed_vm_index,
                cluster_untyped_candidates=cluster_untyped_vm_candidates,
            )
            if existing is not None:
                existing_name = existing.get("name")
                if isinstance(existing_name, str) and existing_name in used:
                    used.discard(existing_name)

        for prepared in sorted(group, key=_sort_key):
            try:
                vmid = int(prepared.resource.get("vmid", 0) or 0)
            except (TypeError, ValueError):
                vmid = 0
            candidate = str(prepared.desired_payload.get("name") or "")
            if not candidate or vmid == 0:
                logger.warning(
                    "Skipping VM name pre-pass for vmid=%s: empty name (name=%r). "
                    "The VM record may be created/left without a resolved name.",
                    vmid or "?",
                    candidate,
                )
                continue

            existing = _select_existing_vm_record(
                prepared=prepared,
                endpoint_id=extract_proxmox_endpoint_id(prepared.desired_payload),
                cluster_id=cluster_id,
                proxmox_vmid=vmid,
                endpoint_typed_index=endpoint_typed_vm_index,
                endpoint_untyped_candidates=endpoint_untyped_vm_candidates,
                cluster_typed_index=cluster_typed_vm_index,
                cluster_untyped_candidates=cluster_untyped_vm_candidates,
            )
            resolution = await resolve_unique_vm_name(
                None,
                netbox_cluster_id=cluster_id,
                proxmox_cluster_name=prepared.cluster_name,
                candidate=candidate,
                proxmox_vmid=vmid,
                used_names_in_cluster=used,
                existing_vm_by_vmid={vmid: existing} if existing is not None else {},
                last_synced_proxmox_name=(
                    last_synced_names.get(_record_id(existing)) if existing is not None else None
                ),
            )

            if resolution.operator_renamed:
                prepared.desired_payload["name"] = resolution.resolved_name
            elif resolution.is_collision:
                prepared.desired_payload["name"] = resolution.resolved_name

            if resolution.is_collision or resolution.operator_renamed:
                resolutions.append(resolution)
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


async def _dispatch_vm_operation_queue(
    nb: object,
    operation_queue: list[_NetBoxVMOperation],
    *,
    overwrite_vm_custom_fields: bool = True,
    custom_fields_enabled_flag: bool | None = None,
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

    async def _run_single(operation: _NetBoxVMOperation) -> None:
        vmid = int(operation.prepared.resource.get("vmid", 0) or 0)
        key = _prepared_vm_result_key(operation.prepared)
        async with write_semaphore:
            try:
                if operation.method == "GET":
                    if operation.existing_record is not None:
                        resolved_records[key] = operation.existing_record
                    return

                if operation.method == "CREATE":
                    existing_resolution = await resolve_virtual_machine_by_sync_state(
                        nb,
                        proxmox_vm_id=vmid,
                        endpoint_id=extract_proxmox_endpoint_id(operation.prepared.desired_payload),
                        cluster_id=_relation_id(operation.prepared.desired_payload.get("cluster")),
                        fallback_query=legacy_custom_field_fallback_query(
                            operation.prepared.lookup,
                            enabled=custom_fields_enabled_flag,
                        ),
                        fail_on_ambiguous=True,
                    )
                    netbox_create_payload = legacy_custom_fields_payload(
                        operation.prepared.desired_payload,
                        overwrite=overwrite_vm_custom_fields,
                        enabled=custom_fields_enabled_flag,
                        context="legacy VM custom-field payload",
                    )
                    if existing_resolution is not None:
                        reconciled = await rest_reconcile_async(
                            nb,
                            "/api/virtualization/virtual-machines/",
                            lookup=operation.prepared.lookup,
                            payload=netbox_create_payload,
                            schema=NetBoxVirtualMachineCreateBody,
                            current_normalizer=lambda record: (
                                _normalize_current_virtual_machine_payload(
                                    record,
                                    supports_virtual_machine_type_field=True,
                                )
                            ),
                            strict_lookup=True,
                            existing_record=existing_resolution.record,
                        )
                        resolved_records[key] = _to_mapping(reconciled)
                        return
                    try:
                        created = await rest_create_async(
                            nb,
                            "/api/virtualization/virtual-machines/",
                            netbox_create_payload,
                            lookup=operation.prepared.lookup,
                        )
                        resolved_records[key] = _to_mapping(created)
                    except ProxboxException:
                        fallback_lookup = legacy_custom_field_fallback_query(
                            operation.prepared.lookup,
                            enabled=custom_fields_enabled_flag,
                        )
                        if fallback_lookup is None:
                            raise
                        existing = await rest_first_async(
                            nb,
                            "/api/virtualization/virtual-machines/",
                            query={**fallback_lookup, "limit": 2},
                        )
                        if existing is None:
                            raise
                        resolved_records[key] = _to_mapping(existing)
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

                patch_payload = legacy_custom_fields_payload(
                    operation.patch_payload,
                    overwrite=overwrite_vm_custom_fields,
                    enabled=custom_fields_enabled_flag,
                    context="legacy VM custom-field payload",
                )
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


def _filter_cluster_resources_for_vm(  # noqa: C901
    cluster_resources: list[dict],
    *,
    vm_name: str,
    proxmox_vm_id: int | None,
    endpoint_id: int | None = None,
    endpoint_id_by_cluster: dict[str, int] | None = None,
    cluster_name: str | None = None,
    cluster_id: int | None = None,
) -> list[dict]:
    """Filter cluster resources to find VM matching name and/or ID criteria.

    Searches through cluster resource lists to find QEMU VMs or LXC containers
    matching the provided identifiers. Optionally filters by cluster name/ID.

    Args:
        cluster_resources: List of cluster resource dicts from Proxmox
        vm_name: VM name to match (exact match)
        proxmox_vm_id: Proxmox VM ID to match, or None
        cluster_name: Cluster name to filter by, or None for all clusters
        cluster_id: NetBox cluster ID to filter by, or None for all

    Returns:
        Filtered list of cluster resource dicts containing matching VMs
    """
    cluster_hint = (cluster_name or "").strip().lower()
    filtered: list[dict] = []
    for cluster in cluster_resources:
        if not isinstance(cluster, dict):
            continue
        for cluster_key, resources in cluster.items():
            if not isinstance(resources, list):
                continue
            cluster_key_str = str(cluster_key)
            if cluster_hint and cluster_key_str.strip().lower() != cluster_hint:
                continue
            if endpoint_id is not None and endpoint_id_by_cluster:
                cluster_endpoint_id = endpoint_id_by_cluster.get(cluster_key_str)
                if cluster_endpoint_id is not None and cluster_endpoint_id != endpoint_id:
                    continue
            selected = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                if resource.get("type") not in ("qemu", "lxc"):
                    continue
                same_name = bool(vm_name) and str(resource.get("name", "")).strip() == vm_name
                same_vmid = proxmox_vm_id is not None and str(
                    resource.get("vmid", "")
                ).strip() == str(proxmox_vm_id)
                if not (same_name or same_vmid):
                    continue
                if cluster_id is not None:
                    resource_cluster_id = _relation_id(resource.get("cluster"))
                    if resource_cluster_id is not None and resource_cluster_id != cluster_id:
                        continue
                selected.append(resource)
            if selected:
                filtered.append({cluster_key_str: selected})
    return filtered


async def _filter_cluster_resources_by_netbox_vm_ids(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    cluster_resources: list[dict],
    netbox_vm_ids: list[int],
) -> list[dict]:
    """Filter cluster resources to only include VMs matching the given NetBox VM IDs."""
    from proxbox_api.netbox_rest import rest_list_async

    if not netbox_vm_ids:
        return cluster_resources

    id_to_vm: dict[int, dict] = {}
    for vm_id in netbox_vm_ids:
        id_to_vm[vm_id] = {"id": vm_id, "name": None, "cluster": None, "cf_proxmox_vm_id": None}

    try:
        vms = await rest_list_async(
            netbox_session,
            "/api/virtualization/virtual-machines/",
            query={"id": ",".join(str(vid) for vid in netbox_vm_ids)},
        )
        if vms and isinstance(vms, list):
            for vm in vms:
                if not isinstance(vm, dict):
                    continue
                vm_id = vm.get("id")
                if vm_id is not None:
                    id_to_vm[vm_id] = vm
    except Exception:
        pass

    target_proxmox_vm_ids: set[int] = set()
    target_vm_names: set[str] = set()
    target_cluster_ids: set[int] = set()

    for vm in id_to_vm.values():
        cf = vm.get("custom_fields", {}) or {}
        raw_vmid = cf.get("proxmox_vm_id")
        if raw_vmid is not None and str(raw_vmid).strip().isdigit():
            target_proxmox_vm_ids.add(int(str(raw_vmid).strip()))
        vm_name = str(vm.get("name", "")).strip()
        if vm_name:
            target_vm_names.add(vm_name.lower())
        cluster = vm.get("cluster")
        if isinstance(cluster, dict):
            cluster_id = cluster.get("id")
            if isinstance(cluster_id, int):
                target_cluster_ids.add(cluster_id)

    filtered: list[dict] = []
    for cluster in cluster_resources:
        if not isinstance(cluster, dict):
            continue
        for cluster_key, resources in cluster.items():
            if not isinstance(resources, list):
                continue
            selected = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                if resource.get("type") not in ("qemu", "lxc"):
                    continue
                res_vmid = resource.get("vmid")
                if res_vmid is not None and int(res_vmid) in target_proxmox_vm_ids:
                    selected.append(resource)
                    continue
                res_name = str(resource.get("name", "")).strip().lower()
                if res_name in target_vm_names:
                    selected.append(resource)
                    continue
            if selected:
                filtered.append({cluster_key: selected})

    return filtered


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

    query: dict[str, object] = {"cf_proxmox_vm_id": vmid}
    if resolved_endpoint_id is not None:
        query["cf_proxmox_endpoint_id"] = resolved_endpoint_id
    elif resolved_cluster_id is not None:
        query["cluster_id"] = resolved_cluster_id
    else:
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
            fallback_query=legacy_custom_field_fallback_query(query),
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
                payload=legacy_custom_fields_payload(
                    {
                        "vid": vlan_tag,
                        "name": f"VLAN {vlan_tag}",
                        "status": "active",
                        "tags": tag_refs,
                        "custom_fields": {"proxmox_last_updated": now.isoformat()},
                    },
                    overwrite=True,
                    context="legacy VLAN custom-field payload",
                ),
                schema=NetBoxVlanSyncState,
                current_normalizer=lambda record: {
                    "vid": record.get("vid"),
                    "name": record.get("name"),
                    "status": record.get("status"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
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
        "custom_fields": {
            "proxmox_last_updated": now.isoformat(),
            **({"proxbox_bridge": bridge_id} if bridge_id is not None else {}),
        },
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
        payload=legacy_custom_fields_payload(
            payload,
            overwrite=(
                overwrite_flags is None or overwrite_flags.overwrite_vm_interface_custom_fields
            ),
            context="legacy VM-interface custom-field payload",
        ),
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
            "custom_fields": record.get("custom_fields"),
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
    custom_fields = payload.get("custom_fields")
    await write_vm_interface_sync_state(
        nb,
        vm_interface_id=interface_id,
        proxbox_bridge_id=(
            custom_fields.get("proxbox_bridge") if isinstance(custom_fields, dict) else None
        ),
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
            payload=legacy_custom_fields_payload(
                {
                    "virtual_machine": virtual_machine.get("id"),
                    "name": disk_entry.name,
                    "size": disk_entry.size_mb,
                    "storage": storage_id,
                    "description": disk_entry.description,
                    "tags": tag_refs,
                    "custom_fields": {"proxmox_last_updated": now.isoformat()},
                },
                overwrite=True,
                context="legacy virtual-disk custom-field payload",
            ),
            schema=NetBoxVirtualDiskSyncState,
            current_normalizer=lambda record: {
                "virtual_machine": record.get("virtual_machine"),
                "name": record.get("name"),
                "size": record.get("size") if record.get("size") is not None else 0,
                "storage": record.get("storage"),
                "description": record.get("description"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
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
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_websocket: bool = False,
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
        custom_fields: Custom field configurations.
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
    vm_name = str(vm_data.get("name", "")).strip()
    vm_cluster_name = _relation_name(vm_data.get("cluster"))
    vm_cluster_id = _relation_id(vm_data.get("cluster"))
    vm_endpoint_id = extract_proxmox_endpoint_id(vm_data)
    cf = vm_data.get("custom_fields")
    proxmox_vm_id = None
    if isinstance(cf, dict):
        raw_id = cf.get("proxmox_vm_id")
        if raw_id is not None and str(raw_id).strip().isdigit():
            proxmox_vm_id = int(str(raw_id).strip())

    # A NetBox VM row can exist with a blank name -- for example after a partial
    # prior sync, or when Proxmox briefly reported an empty name during a rename.
    # Such a record is still matchable as long as the Proxmox VM ID is known,
    # because _filter_cluster_resources_for_vm matches on name OR vmid and the
    # downstream create/update flow heals the name from the matched Proxmox
    # resource. Only refuse the sync when neither a name nor a proxmox_vm_id is
    # available to match on.
    if not vm_name and proxmox_vm_id is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Virtual machine id={netbox_vm_id} has no name and no "
                "proxmox_vm_id custom field to match in Proxmox."
            ),
        )

    filtered_resources = _filter_cluster_resources_for_vm(
        cluster_resources,
        vm_name=vm_name,
        proxmox_vm_id=proxmox_vm_id,
        endpoint_id=vm_endpoint_id,
        endpoint_id_by_cluster=_endpoint_id_by_cluster_names(pxs, cluster_status),
        cluster_name=vm_cluster_name,
        cluster_id=vm_cluster_id,
    )
    if not filtered_resources:
        raise HTTPException(
            status_code=404,
            detail=(
                "No matching Proxmox VM was found for NetBox virtual machine "
                f"id={netbox_vm_id} (name={vm_name!r}, proxmox_vm_id={proxmox_vm_id})."
            ),
        )

    filtered_for_call = filtered_resources

    return await create_virtual_machines(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=filtered_for_call,
        custom_fields=custom_fields,
        tag=tag,
        websocket=websocket,
        use_websocket=use_websocket,
        use_guest_agent_interface_name=use_guest_agent_interface_name,
        vm_interface_sync_strategy=vm_interface_sync_strategy,
        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
        primary_ip_preference=primary_ip_preference,
        overwrite_vm_role=overwrite_vm_role,
        overwrite_vm_type=overwrite_vm_type,
        overwrite_vm_tags=overwrite_vm_tags,
        overwrite_vm_description=overwrite_vm_description,
        overwrite_vm_custom_fields=overwrite_vm_custom_fields,
        overwrite_flags=overwrite_flags if overwrite_flags is not None else SyncOverwriteFlags(),
        run_id=run_id,
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
            custom_fields={
                "proxmox_vm_id": 100,
                "proxmox_start_at_boot": True,
                "proxmox_unprivileged_container": False,
                "proxmox_qemu_agent": True,
                "proxmox_search_domain": "example.com",
            },
        )
    )

    return virtual_machine


@router.get(
    "/create",
    dependencies=[
        Depends(ensure_netbox_sync_dependencies),
        Depends(reset_sidecar_availability_cache),
    ],
)
async def create_virtual_machines(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_css: bool = False,
    use_websocket: bool = False,
    sync_vm_network: bool = True,
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
        title="Overwrite VM Custom Fields",
        description=(
            "When false, custom_fields are not patched on existing VMs that already have "
            "non-empty custom_fields. Custom fields are still applied on first create. "
            "When unset, falls back to overwrite_flags.overwrite_vm_custom_fields."
        ),
    ),
    overwrite_flags: ResolvedSyncOverwriteFlagsDep = SyncOverwriteFlags(),
    behavior_flags: ResolvedSyncBehaviorFlagsDep = SyncBehaviorFlags(),
    run_id: str | None = Query(
        default=None,
        title="Run ID",
        description=(
            "UUID stamped on each touched VM's proxbox_last_run_id custom field. "
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
        custom_fields: Custom field configurations for NetBox.
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
    effective_vm_overwrite_flags = overwrite_flags.model_copy(
        update={
            "overwrite_vm_role": overwrite_vm_role,
            "overwrite_vm_type": overwrite_vm_type,
            "overwrite_vm_tags": overwrite_vm_tags,
            "overwrite_vm_description": overwrite_vm_description,
            "overwrite_vm_custom_fields": overwrite_vm_custom_fields,
        }
    )
    sync_mode_vm = _normalize_sync_mode(sync_mode_vm, "sync_mode_vm")
    sync_mode_vm_template = _normalize_sync_mode(sync_mode_vm_template, "sync_mode_vm_template")
    normalized_interface_strategy = normalize_vm_interface_sync_strategy(vm_interface_sync_strategy)
    if normalized_interface_strategy == "legacy_rename":
        warn_legacy_vm_interface_strategy()
    nb = netbox_session
    netbox_version = await detect_netbox_version(nb)
    supports_vm_type = supports_virtual_machine_type(netbox_version)
    vm_patchable_set = _compute_vm_patchable_fields(
        effective_vm_overwrite_flags,
        supports_virtual_machine_type_field=supports_vm_type,
    )
    if not include_custom_fields_in_payload(
        overwrite_vm_custom_fields,
        enabled=behavior_flags.custom_fields_enabled,
        context="legacy VM custom-field payload",
    ):
        vm_patchable_set.discard("custom_fields")
    vm_patchable_fields = frozenset(vm_patchable_set)
    effective_run_id = run_id if isinstance(run_id, str) and run_id else str(uuid.uuid4())

    filtered_cluster_resources = cluster_resources
    bridge: WebSocketSSEBridge | None = (
        websocket if isinstance(websocket, WebSocketSSEBridge) else None
    )

    if netbox_vm_ids and isinstance(netbox_vm_ids, str):
        vm_ids = parse_comma_separated_ints(netbox_vm_ids)
        if vm_ids:
            filtered_cluster_resources = await _filter_cluster_resources_by_netbox_vm_ids(
                netbox_session=netbox_session,
                cluster_resources=cluster_resources,
                netbox_vm_ids=vm_ids,
            )

    # Drop VM/template resources disabled by sync mode at the source so they
    # never drive discovery or dependency precompute (see finding: disabled
    # sync modes must not create dependent NetBox objects).
    filtered_cluster_resources = _filter_cluster_resources_by_sync_modes(
        filtered_cluster_resources, sync_mode_vm, sync_mode_vm_template
    )

    # Build a mapping from cluster name to Proxmox base URL for populating proxmox_link,
    # and a parallel mapping to the ProxmoxEndpoint DB ID for the console access custom field.
    proxmox_url_by_cluster: dict[str, str] = {}
    endpoint_id_by_cluster = _endpoint_id_by_cluster_names(pxs, cluster_status)
    px_by_cluster: dict[str, object] = {}
    for px, cs in zip(pxs, cluster_status):
        cluster_n = getattr(cs, "name", None) or getattr(px, "cluster_name", None)
        px_domain = getattr(px, "domain", None) or getattr(px, "ip_address", None) or ""
        px_port = getattr(px, "http_port", 8006)
        if cluster_n and px_domain:
            proxmox_url_by_cluster[str(cluster_n)] = f"https://{px_domain}:{px_port}"
        for px_name in {
            cluster_n,
            getattr(px, "name", None),
            getattr(px, "cluster_name", None),
            getattr(px, "node_name", None),
        }:
            px_name_text = str(px_name or "").strip()
            if px_name_text:
                px_by_cluster[px_name_text] = px

    # Per-cluster color-map cache: fetched once on first VM that needs it.
    tag_color_map_by_cluster: dict[str, dict[str, str]] = {}
    tag_color_map_locks: dict[str, asyncio.Lock] = {}

    async def _get_cluster_tag_color_map(cluster_name: str) -> dict[str, str]:
        if cluster_name in tag_color_map_by_cluster:
            return tag_color_map_by_cluster[cluster_name]
        lock = tag_color_map_locks.setdefault(cluster_name, asyncio.Lock())
        async with lock:
            if cluster_name in tag_color_map_by_cluster:
                return tag_color_map_by_cluster[cluster_name]
            px = px_by_cluster.get(cluster_name)
            if px is None:
                tag_color_map_by_cluster[cluster_name] = {}
                return tag_color_map_by_cluster[cluster_name]
            tag_color_map_by_cluster[cluster_name] = await fetch_tag_color_map(px)
            return tag_color_map_by_cluster[cluster_name]

    async def _resolve_vm_proxmox_tag_ids(cluster_name: str, vm_config: dict) -> list[int]:
        if not overwrite_flags.overwrite_vm_proxmox_tags:
            return []
        raw = vm_config.get("tags") if isinstance(vm_config, dict) else None
        if not raw:
            return []
        color_map = await _get_cluster_tag_color_map(cluster_name)
        return await resolve_proxmox_tag_ids(nb, raw, color_map=color_map)

    total_vms = 0  # Track total VMs processed
    successful_vms = 0  # Track successful VM creations
    failed_vms = 0  # Track failed VM creations
    tag_refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    tag_refs = [tag_ref for tag_ref in tag_refs if tag_ref.get("name") and tag_ref.get("slug")]
    flattened_results = []
    storage_index: dict[tuple[str, str], dict] = {}
    cluster_dependency_cache: dict[str, dict[str, object]] = {}
    node_device_cache: dict[tuple[str, str], object] = {}
    vm_role_cache: dict[str, object] = {}
    vm_type_cache: dict[str, object] = {}
    vm_role_mapping: dict[str, dict[str, object]] = VM_ROLE_MAPPINGS

    # Emit discovery event immediately if using bridge/SSE streaming.
    # This prevents the stream consumer from hanging while waiting for the first event.
    if bridge:
        vm_items: list[dict[str, object]] = []
        for cluster in filtered_cluster_resources:
            if isinstance(cluster, dict):
                for cluster_name, resources in cluster.items():
                    if isinstance(resources, list):
                        for resource in resources:
                            if isinstance(resource, dict) and resource.get("type") in (
                                "qemu",
                                "lxc",
                            ):
                                vm_items.append(
                                    {
                                        "name": str(
                                            resource.get("name")
                                            or resource.get("vmid")
                                            or "unknown"
                                        ),
                                        "type": str(resource.get("type") or "unknown"),
                                        "cluster": str(cluster_name),
                                        "node": str(resource.get("node") or ""),
                                    }
                                )
        await bridge.emit_discovery(
            phase="virtual-machines",
            items=vm_items,
            message=f"Discovered {len(vm_items)} virtual machine(s) to synchronize",
            metadata={"sync_vm_network": sync_vm_network},
        )

    async def _precompute_vm_dependencies() -> None:
        """Ensure shared dependencies in strict parent-to-child order.

        Dependency chain enforced here:
        manufacturer -> device type -> cluster type -> cluster/site -> node device -> VM role -> VM type.
        """

        resources_by_cluster: dict[str, list[dict]] = {}
        for cluster in filtered_cluster_resources:
            if not isinstance(cluster, dict):
                continue
            for candidate_cluster_name, resources in cluster.items():
                if not isinstance(resources, list):
                    continue
                vm_resources = [
                    resource
                    for resource in resources
                    if isinstance(resource, dict) and resource.get("type") in ("qemu", "lxc")
                ]
                if vm_resources:
                    resources_by_cluster[str(candidate_cluster_name)] = vm_resources

        # Nothing to precompute when no VM resources were discovered.
        if not resources_by_cluster:
            return

        manufacturer = await _ensure_manufacturer(nb, tag_refs=tag_refs)
        device_type = await _ensure_device_type(
            nb,
            manufacturer_id=getattr(manufacturer, "id", None),
            tag_refs=tag_refs,
        )
        device_role = await _ensure_proxmox_node_role(nb, tag_refs=tag_refs)

        vm_types: set[str] = set()

        async def _precompute_single_cluster(cluster_name: str, vm_resources: list[dict]) -> None:
            cluster_state = next(
                (state for state in cluster_status if getattr(state, "name", None) == cluster_name),
                None,
            )
            cluster_mode = getattr(cluster_state, "mode", None) or "cluster"
            # cluster_type, site, and tenant are mutually independent — run them in parallel.
            cluster_type, site, tenant = await asyncio.gather(
                _ensure_cluster_type(nb, mode=cluster_mode, tag_refs=tag_refs),
                _ensure_site(
                    nb,
                    cluster_name=cluster_name,
                    tag_refs=tag_refs,
                    placement=cluster_state,
                ),
                _resolve_tenant(nb, placement=cluster_state),
            )
            cluster = await _ensure_cluster(
                nb,
                cluster_name=cluster_name,
                cluster_type_id=getattr(cluster_type, "id", None),
                mode=cluster_mode,
                tag_refs=tag_refs,
                site_id=getattr(site, "id", None),
                tenant_id=getattr(tenant, "id", None),
                overwrite_flags=overwrite_flags,
            )
            site_id = _effective_cluster_site_id(
                cluster,
                fallback_site_id=getattr(site, "id", None),
            )

            cluster_dependency_cache[cluster_name] = {
                "cluster": cluster,
                "site": site,
                "site_id": site_id,
                "tenant": tenant,
                "device_type": device_type,
                "device_role": device_role,
            }

            node_names = {
                str(resource.get("node"))
                for resource in vm_resources
                if resource.get("node") is not None
            }
            for node_name in sorted(node_names):
                node_device_cache[(cluster_name, node_name)] = await _ensure_device(
                    nb,
                    device_name=node_name,
                    cluster_id=getattr(cluster, "id", None),
                    device_type_id=getattr(device_type, "id", None),
                    role_id=getattr(device_role, "id", None),
                    site_id=site_id,
                    tag_refs=tag_refs,
                    overwrite_device_role=overwrite_flags.overwrite_device_role,
                    overwrite_device_type=overwrite_flags.overwrite_device_type,
                    overwrite_device_tags=overwrite_flags.overwrite_device_tags,
                    overwrite_flags=overwrite_flags,
                )

            for resource in vm_resources:
                vt = str(resource.get("type") or "undefined").lower()
                if vt not in vm_role_mapping:
                    vt = "undefined"
                vm_types.add(vt)

        # All clusters are independent — precompute them in parallel.
        cluster_precompute_results = await asyncio.gather(
            *[_precompute_single_cluster(cn, vrs) for cn, vrs in resources_by_cluster.items()],
            return_exceptions=True,
        )
        # Re-raise the first cluster-level failure so the outer try/except can surface it.
        for _cluster_result in cluster_precompute_results:
            if isinstance(_cluster_result, BaseException):
                raise _cluster_result

        sorted_vm_types = sorted(vm_types)
        role_results = await asyncio.gather(
            *[
                rest_reconcile_async(
                    nb,
                    "/api/dcim/device-roles/",
                    lookup={
                        "slug": vm_role_mapping.get(vt, vm_role_mapping["undefined"]).get("slug")
                    },
                    payload={
                        **vm_role_mapping.get(vt, vm_role_mapping["undefined"]),
                        "tags": tag_refs,
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
                for vt in sorted_vm_types
            ],
            return_exceptions=True,
        )
        for vt, role in zip(sorted_vm_types, role_results):
            if not isinstance(role, BaseException):
                vm_role_cache[vt] = role

        if supports_vm_type:
            type_results = await asyncio.gather(
                *[
                    ensure_vm_type(nb, vt, tag_refs, netbox_version=netbox_version)
                    for vt in sorted_vm_types
                ],
                return_exceptions=True,
            )
            for vt, result in zip(sorted_vm_types, type_results):
                if result is not None and not isinstance(result, BaseException):
                    vm_type_cache[vt] = result

    try:
        storage_records = await rest_list_async(nb, "/api/plugins/proxbox/storage/")
        storage_index = build_storage_index(storage_records)
    except Exception as error:
        error_detail = getattr(error, "detail", str(error))
        error_msg = f"{type(error).__name__}: {error_detail}"
        logger.warning("Error loading storage records for VM sync: %s", error_msg)

    try:
        await _precompute_vm_dependencies()
    except Exception as error:
        error_detail = _vm_dependency_error_detail(error)
        raise ProxboxException(
            message="Error creating Virtual Machine dependent objects (cluster, device, tag and role)",
            detail=error_detail,
            python_exception=f"Error: {error_detail}",
        )

    async def _get_vm_type(vm_type_key: str) -> object | None:
        if not supports_vm_type:
            return None
        if vm_type_key not in vm_type_cache and vm_type_key in VM_TYPE_MAPPINGS:
            result = await ensure_vm_type(nb, vm_type_key, tag_refs, netbox_version=netbox_version)
            if result is not None:
                vm_type_cache[vm_type_key] = result
        return vm_type_cache.get(vm_type_key)

    prepare_context = _VMPreparationContext(
        nb=nb,
        tag=tag,
        overwrite_flags=overwrite_flags,
        behavior_flags=behavior_flags,
        effective_vm_overwrite_flags=effective_vm_overwrite_flags,
        cluster_dependency_cache=cluster_dependency_cache,
        node_device_cache=node_device_cache,
        vm_role_cache=vm_role_cache,
        vm_role_mapping=vm_role_mapping,
        tag_refs=tag_refs,
        proxmox_url_by_cluster=proxmox_url_by_cluster,
        endpoint_id_by_cluster=endpoint_id_by_cluster,
        resolve_vm_type=_get_vm_type,
        resolve_vm_proxmox_tag_ids=_resolve_vm_proxmox_tag_ids,
    )

    async def _run_full_update_vm_batch() -> tuple[list[dict[str, object]], int]:  # noqa: C901
        """Run the batched full-update VM sync.

        Returns ``(synced_records, failed_count)``. ``failed_count`` counts VMs
        that existed in the input but could not be synced (preparation raised, or
        the dispatched operation produced no resolved NetBox record). It is kept
        separate from a legitimately empty input so callers can distinguish
        "nothing to sync" from "everything failed" instead of reporting silent
        success.
        """
        batch_t0 = time.perf_counter()
        operation_inputs: list[tuple[str, dict]] = []
        for cluster in filtered_cluster_resources:
            if not isinstance(cluster, dict):
                continue
            for cluster_name, resources in cluster.items():
                if not isinstance(resources, list):
                    continue
                for resource in resources:
                    # Sync-mode filtering already applied at the source via
                    # _filter_cluster_resources_by_sync_modes.
                    if isinstance(resource, dict) and resource.get("type") in ("qemu", "lxc"):
                        operation_inputs.append((str(cluster_name), resource))

        if not operation_inputs:
            return [], 0

        fetch_semaphore = asyncio.Semaphore(max(1, resolve_vm_sync_concurrency()))

        async def _fetch_with_limit(
            cluster_name: str,
            resource: dict[str, object],
        ) -> dict[str, object]:
            async with fetch_semaphore:
                cluster_px = px_by_cluster.get(str(cluster_name))
                fetch_pxs = [cluster_px] if cluster_px is not None else pxs
                return await _fetch_vm_config_only(pxs=fetch_pxs, resource=resource)

        fetch_t0 = time.perf_counter()
        fetch_results = await asyncio.gather(
            *[
                _fetch_with_limit(cluster_name, resource)
                for cluster_name, resource in operation_inputs
            ],
            return_exceptions=True,
        )
        fetch_ms = (time.perf_counter() - fetch_t0) * 1000

        fetched_vm_configs: list[tuple[str, dict[str, object], dict[str, object]]] = []
        prepared_vms: list[_PreparedVMState] = []
        failed_vms = 0
        fetch_failed = 0
        for (cluster_name, resource), fetch_result in zip(operation_inputs, fetch_results):
            if isinstance(fetch_result, Exception):
                logger.warning(
                    "VM config fetch failed: cluster=%s vmid=%s error=%s",
                    cluster_name,
                    resource.get("vmid"),
                    fetch_result,
                )
                failed_vms += 1
                fetch_failed += 1
                continue
            fetched_vm_configs.append((cluster_name, resource, fetch_result))

        process_t0 = time.perf_counter()
        for cluster_name, resource, vm_config in fetched_vm_configs:
            try:
                prepared_vms.append(
                    await _prepare_vm_from_config(
                        cluster_name,
                        resource,
                        vm_config,
                        prepare_context,
                    )
                )
            except Exception as prepared_result:
                logger.warning(
                    "VM preparation failed: cluster=%s vmid=%s error=%s",
                    cluster_name,
                    resource.get("vmid"),
                    prepared_result,
                )
                failed_vms += 1
        process_ms = (time.perf_counter() - process_t0) * 1000

        logger.info(
            "VM full-update phase timing: fetch_ms=%.2f process_ms=%.2f "
            "fetched_ok=%d fetch_failed=%d",
            fetch_ms,
            process_ms,
            len(fetched_vm_configs),
            fetch_failed,
        )

        if not prepared_vms:
            return [], failed_vms

        netbox_snapshot = await _load_netbox_virtual_machine_snapshot(nb, fresh=True)
        await _hydrate_vm_snapshot_with_sidecar_identity(
            nb,
            prepared_vms=prepared_vms,
            netbox_snapshot=netbox_snapshot,
            custom_fields_enabled_flag=behavior_flags.custom_fields_enabled,
        )
        await _resolve_vm_names_pre_pass(prepared_vms, netbox_snapshot, bridge, nb)
        reconciliation_t0 = time.perf_counter()
        operation_queue = _build_vm_operation_queue(
            prepared_vms,
            netbox_snapshot,
            overwrite_vm_role=overwrite_vm_role,
            overwrite_vm_type=overwrite_vm_type,
            overwrite_vm_tags=overwrite_vm_tags,
            overwrite_vm_description=overwrite_vm_description,
            overwrite_vm_custom_fields=overwrite_vm_custom_fields,
            supports_virtual_machine_type_field=supports_vm_type,
        )
        reconciliation_ms = (time.perf_counter() - reconciliation_t0) * 1000
        _log_vm_reconciliation_measurement(
            operation_queue=operation_queue,
            prepared_vms=prepared_vms,
            netbox_snapshot=netbox_snapshot,
            duration_ms=reconciliation_ms,
            supports_virtual_machine_type_field=supports_vm_type,
        )

        resolved_records, failed_operation_keys = await _dispatch_vm_operation_queue(
            nb,
            operation_queue,
            overwrite_vm_custom_fields=overwrite_vm_custom_fields,
            custom_fields_enabled_flag=behavior_flags.custom_fields_enabled,
        )

        results: list[dict[str, object]] = []
        for operation in operation_queue:
            vmid = int(operation.prepared.resource.get("vmid", 0) or 0)
            key = _prepared_vm_result_key(operation.prepared)
            # A dispatch failure for this VM is counted as failed even when a
            # stale existing record is available, so it is never masked as success.
            if key in failed_operation_keys:
                failed_vms += 1
                continue
            vm_record = resolved_records.get(key)
            if vm_record is None and operation.existing_record is not None:
                vm_record = operation.existing_record
            if vm_record is None:
                logger.warning(
                    "VM operation completed without resolved NetBox record: cluster=%s vmid=%s method=%s",
                    operation.prepared.cluster_name,
                    vmid,
                    operation.method,
                )
                failed_vms += 1
                continue
            await stamp_vm_last_run_id(nb, vm_record, effective_run_id)
            desired_custom_fields = operation.prepared.desired_payload.get("custom_fields")
            await write_virtual_machine_sync_state(
                nb,
                virtual_machine_id=vm_record.get("id"),
                custom_fields=(
                    desired_custom_fields if isinstance(desired_custom_fields, dict) else None
                ),
                overwrite_custom_fields=overwrite_vm_custom_fields,
                # The live Proxmox name, NOT desired_payload["name"] -- the name
                # resolver may have rewritten that to preserve an operator's
                # NetBox-side rename, and recording it here would cement the
                # stale name as "what Proxmox last said".
                proxmox_vm_name=operation.prepared.resource.get("name"),
            )
            results.append(vm_record)

            vm_id = _relation_id(vm_record.get("id"))
            if vm_id is None:
                continue
            try:
                await sync_virtual_machine_task_history(
                    netbox_session=nb,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    virtual_machine_id=vm_id,
                    proxmox_vmid=vmid,
                    vm_type=str(operation.prepared.vm_type or "unknown"),
                    cluster_name=operation.prepared.cluster_name,
                    tag_refs=tag_refs,
                    websocket=websocket,
                    use_websocket=use_websocket,
                )
            except Exception as error:
                logger.warning(
                    "Error syncing task history for VM %s (%s): %s",
                    operation.prepared.resource.get("name"),
                    operation.prepared.resource.get("vmid"),
                    error,
                )

        batch_ms = (time.perf_counter() - batch_t0) * 1000
        reconciliation_share_pct = (reconciliation_ms / batch_ms) * 100 if batch_ms > 0 else 0.0
        logger.info(
            "VM full-update batch timing: total_ms=%.2f reconciliation_ms=%.2f "
            "fetch_ms=%.2f process_ms=%.2f reconciliation_share_pct=%.2f "
            "vm_count=%d snapshot_count=%d",
            batch_ms,
            reconciliation_ms,
            fetch_ms,
            process_ms,
            reconciliation_share_pct,
            len(prepared_vms),
            len(netbox_snapshot),
        )

        return results, failed_vms

    if not sync_vm_network:
        flattened_results, failed_vms = await _run_full_update_vm_batch()
        successful_vms = len(flattened_results)
        total_vms = successful_vms + failed_vms
        if bridge:
            summary_message = (
                f"Virtual machine sync completed: {successful_vms} synchronized"
                if failed_vms == 0
                else (
                    "Virtual machine sync completed with errors: "
                    f"{successful_vms} synchronized, {failed_vms} failed"
                )
            )
            await bridge.emit_phase_summary(
                phase="virtual-machines",
                created=successful_vms,
                failed=failed_vms,
                message=summary_message,
            )
        if all([use_websocket, websocket]):
            await websocket.send_json({"object": "virtual_machine", "end": True})
        global_cache.clear_cache()
        logger.info(
            "VM sync summary: total=%s ok=%s failed=%s",
            total_vms,
            successful_vms,
            failed_vms,
        )
        return flattened_results

    async def create_vm_task(cluster_name, resource):  # noqa: C901
        undefined_html = return_status_html("undefined", use_css)

        websocket_vm_json: dict = {
            "sync_status": return_status_html("syncing", use_css),
            "name": undefined_html,
            "netbox_id": undefined_html,
            "status": undefined_html,
            "cluster": undefined_html,
            "device": undefined_html,
            "role": undefined_html,
            "vcpus": undefined_html,
            "memory": undefined_html,
            "disk": undefined_html,
            "vm_interfaces": undefined_html,
        }

        vm_type = resource.get("type", "unknown")
        vm_type_key = str(vm_type).lower() if vm_type else "undefined"
        if vm_type_key not in vm_role_mapping:
            vm_type_key = "undefined"
        vm_config_result = get_vm_config(
            pxs=pxs,
            node=resource.get("node"),
            type=vm_type,
            vmid=resource.get("vmid"),
        )
        if inspect.isawaitable(vm_config_result):
            vm_config_result = await vm_config_result
        vm_config = vm_config_result

        if vm_config is None:
            vm_config = {}
        vm_config_obj = ProxmoxVmConfigInput.model_validate(vm_config)

        initial_vm_json = websocket_vm_json | {
            "completed": False,
            "rowid": str(resource.get("name")),
            "name": str(resource.get("name")),
            "cluster": str(cluster_name),
            "device": str(resource.get("node")),
        }

        vm_name = str(resource.get("name") or resource.get("vmid") or "unknown")
        timing_key = f"vm_{cluster_name}_{resource.get('vmid')}"
        if bridge:
            bridge.start_timer(timing_key)
            await bridge.emit_item_progress(
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

        if all([use_websocket, websocket]):
            await websocket.send_json(
                {"object": "virtual_machine", "type": "create", "data": initial_vm_json}
            )

        try:
            if bridge:
                await bridge.emit_substep(
                    phase="virtual-machines",
                    substep="resolve_dependencies",
                    status=SubstepStatus.PROCESSING,
                    message=f"Resolving dependencies for VM '{vm_name}'",
                    item={"name": vm_name},
                )
            cluster_dependencies = cluster_dependency_cache.get(str(cluster_name), {})
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

            node_name = str(resource.get("node"))
            site_id = _cluster_dependency_site_id(cluster_dependencies)
            device = node_device_cache.get((str(cluster_name), node_name))
            if device is None:
                # Fallback for edge cases where a node appears after preflight filtering.
                device = await _ensure_device(
                    nb,
                    device_name=node_name,
                    cluster_id=getattr(cluster, "id", None),
                    device_type_id=getattr(cluster_dependencies.get("device_type"), "id", None),
                    role_id=getattr(cluster_dependencies.get("device_role"), "id", None),
                    site_id=site_id,
                    tag_refs=tag_refs,
                    overwrite_device_role=overwrite_flags.overwrite_device_role,
                    overwrite_device_type=overwrite_flags.overwrite_device_type,
                    overwrite_device_tags=overwrite_flags.overwrite_device_tags,
                    overwrite_flags=overwrite_flags,
                )
                node_device_cache[(str(cluster_name), node_name)] = device

            role = vm_role_cache.get(vm_type_key)
            if role is None:
                role_payload = vm_role_mapping.get(vm_type_key, vm_role_mapping["undefined"])
                role = await rest_reconcile_async(
                    nb,
                    "/api/dcim/device-roles/",
                    lookup={"slug": role_payload.get("slug")},
                    payload={
                        **role_payload,
                        "tags": tag_refs,
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
                vm_role_cache[vm_type_key] = role

            vm_type_obj = await _get_vm_type(vm_type_key)
            vm_type_id = int(getattr(vm_type_obj, "id", 0) or 0) if vm_type_obj else None

            logger.debug("VM deps cluster=%s device=%s role=%s", cluster, device, role)
            if bridge:
                await bridge.emit_substep(
                    phase="virtual-machines",
                    substep="resolve_dependencies",
                    status=SubstepStatus.COMPLETED,
                    message=f"Dependencies ready for VM '{vm_name}'",
                    item={"name": vm_name},
                    timing_key=timing_key,
                )

        except Exception as error:
            error_detail = _vm_dependency_error_detail(error)
            if bridge:
                await bridge.emit_error_detail(
                    message="Failed to resolve VM dependencies",
                    category=ErrorCategory.VALIDATION,
                    phase="virtual-machines",
                    item={"name": vm_name},
                    detail=error_detail,
                    suggestion="Check cluster, node device, and VM role mappings in NetBox",
                )
            raise ProxboxException(
                message="Error creating Virtual Machine dependent objects (cluster, device, tag and role)",
                detail=error_detail,
                python_exception=f"Error: {error_detail}",
            )

        now = datetime.now(timezone.utc)
        proxmox_tag_ids = await _resolve_vm_proxmox_tag_ids(str(cluster_name), vm_config)
        proxbox_tag_id = int(getattr(tag, "id", 0) or 0)
        merged_tag_ids = sorted({proxbox_tag_id, *proxmox_tag_ids} - {0})
        netbox_vm_payload = build_netbox_virtual_machine_payload(
            proxmox_resource=resource,
            proxmox_config=vm_config,
            cluster_id=int(getattr(cluster, "id", 0) or 0),
            device_id=int(getattr(device, "id", 0) or 0),
            role_id=None if vm_type_id else int(getattr(role, "id", 0) or 0),
            tag_ids=merged_tag_ids,
            site_id=site_id,
            tenant_id=int(getattr(cluster_dependencies.get("tenant"), "id", 0) or 0) or None,
            virtual_machine_type_id=vm_type_id,
            last_updated=now,
            cluster_name=str(cluster_name),
            proxmox_url=proxmox_url_by_cluster.get(str(cluster_name)),
            endpoint_id=endpoint_id_by_cluster.get(str(cluster_name)),
            parse_description_metadata=behavior_flags.parse_description_metadata,
            overwrite_flags=effective_vm_overwrite_flags,
        )

        if bridge:
            await bridge.emit_substep(
                phase="virtual-machines",
                substep="reconcile_vm",
                status=SubstepStatus.PROCESSING,
                message=f"Reconciling VM '{vm_name}' in NetBox",
                item={"name": vm_name},
            )

        vm_lookup = _vm_identity_lookup(
            vmid=resource.get("vmid"),
            endpoint_id=endpoint_id_by_cluster.get(str(cluster_name)),
            cluster_id=int(getattr(cluster, "id", 0) or 0) or None,
        )
        existing_resolution = await resolve_virtual_machine_by_sync_state(
            nb,
            proxmox_vm_id=resource.get("vmid"),
            endpoint_id=endpoint_id_by_cluster.get(str(cluster_name)),
            cluster_id=int(getattr(cluster, "id", 0) or 0) or None,
            fallback_query=legacy_custom_field_fallback_query(
                vm_lookup,
                enabled=behavior_flags.custom_fields_enabled,
            ),
            fail_on_ambiguous=True,
        )
        virtual_machine = await rest_reconcile_async(
            nb,
            "/api/virtualization/virtual-machines/",
            lookup=vm_lookup,
            payload=legacy_custom_fields_payload(
                netbox_vm_payload,
                overwrite=overwrite_vm_custom_fields,
                enabled=behavior_flags.custom_fields_enabled,
                context="legacy VM custom-field payload",
            ),
            schema=NetBoxVirtualMachineCreateBody,
            patchable_fields=vm_patchable_fields,
            current_normalizer=lambda record: _normalize_current_virtual_machine_payload(
                record,
                supports_virtual_machine_type_field=supports_vm_type,
            ),
            strict_lookup=True,
            existing_record=(
                existing_resolution.record if existing_resolution is not None else None
            ),
        )

        await stamp_vm_last_run_id(nb, virtual_machine, effective_run_id)
        virtual_machine_id = (
            virtual_machine.get("id")
            if isinstance(virtual_machine, dict)
            else getattr(virtual_machine, "id", None)
        )
        desired_custom_fields = netbox_vm_payload.get("custom_fields")
        await write_virtual_machine_sync_state(
            nb,
            virtual_machine_id=virtual_machine_id,
            custom_fields=desired_custom_fields
            if isinstance(desired_custom_fields, dict)
            else None,
            overwrite_custom_fields=overwrite_vm_custom_fields,
        )

        logger.debug("Reconciled virtual_machine=%s", virtual_machine)
        if bridge:
            await bridge.emit_substep(
                phase="virtual-machines",
                substep="reconcile_vm",
                status=SubstepStatus.COMPLETED,
                message=f"VM '{vm_name}' reconciled in NetBox",
                item={"name": vm_name},
                timing_key=timing_key,
            )

        if not isinstance(virtual_machine, dict):
            virtual_machine = virtual_machine.dict()

        # Create VM interfaces
        netbox_vm_interfaces = []
        first_ipv4_id: int | None = None
        first_ipv6_id: int | None = None
        total_interface_count = 0
        failed_interface_count = 0
        if virtual_machine and vm_config and sync_vm_network:
            guest_agent_interfaces: list[dict] = []
            guest_agent_diagnostic: str | None = None
            if vm_type == "qemu" and vm_config_obj.qemu_agent_enabled:
                proxmox_session = next(
                    (
                        px
                        for px, cluster in zip(pxs, cluster_status)
                        if getattr(cluster, "name", None) == cluster_name
                    ),
                    None,
                )
                if proxmox_session is not None:
                    guest_agent_result = await fetch_qemu_guest_agent_network_interfaces(
                        proxmox_session,
                        node=str(resource.get("node")),
                        vmid=int(resource.get("vmid")),
                    )
                    guest_agent_interfaces = guest_agent_result.interfaces
                    guest_agent_diagnostic = guest_agent_result.diagnostic
                    if not guest_agent_interfaces:
                        logger.info(
                            "Guest agent network data unavailable for VM %s (vmid=%s); falling back to config networks. (%s)",
                            resource.get("name"),
                            resource.get("vmid"),
                            guest_agent_diagnostic or "no interfaces returned",
                        )
                        if bridge and guest_agent_diagnostic:
                            await bridge.emit_substep(
                                phase="virtual-machines",
                                substep="vm_interfaces",
                                status=SubstepStatus.COMPLETED,
                                message=(
                                    f"VM '{vm_name}' guest-agent IPs unavailable: "
                                    f"{guest_agent_diagnostic}"
                                ),
                                item={"name": vm_name, "vmid": resource.get("vmid")},
                                timing_key=timing_key,
                            )

            guest_by_name = {
                str(iface.get("name", "")).strip().lower(): iface
                for iface in guest_agent_interfaces
            }
            guest_by_mac = build_guest_mac_index(guest_agent_interfaces)

            vm_networks = _parse_vm_networks(vm_config)

            vm_dns_name = await _resolve_vm_dns_name(
                proxmox_session=next(
                    (
                        px
                        for px, cluster in zip(pxs, cluster_status)
                        if getattr(cluster, "name", None) == cluster_name
                    ),
                    None,
                ),
                node=str(resource.get("node") or "") or None,
                vmid=resource.get("vmid"),
                vm_type=vm_type,
                vm_config=vm_config,
            )

            if vm_networks:
                # Build interface kwargs up front so each interface can be
                # retried independently on a transient NetBox failure. Storing
                # kwargs (instead of pre-created coroutines) lets the retry
                # helper re-invoke the creation cleanly.
                interface_kwargs: list[dict] = []
                for network in vm_networks:
                    for interface_name, value in network.items():
                        config_interface_name = (
                            str(value.get("name", interface_name)).strip() or interface_name
                        )
                        interface_mac = value.get("virtio", value.get("hwaddr", None))
                        guest_iface = None
                        if interface_mac:
                            guest_iface = merged_guest_iface_from_mac_index(
                                guest_by_mac, interface_mac
                            )
                        if guest_iface is None:
                            guest_iface = guest_by_name.get(config_interface_name.lower())
                        resolved_interface_name = config_interface_name
                        if (
                            should_use_guest_agent_core_interface_name(
                                use_guest_agent_interface_name,
                                normalized_interface_strategy,
                            )
                            and guest_iface
                        ):
                            guest_name = str(guest_iface.get("name") or "").strip()
                            if guest_name:
                                resolved_interface_name = guest_name

                        interface_kwargs.append(
                            dict(
                                nb=nb,
                                virtual_machine=virtual_machine,
                                interface_name=resolved_interface_name,
                                interface_config=value,
                                guest_iface=guest_iface,
                                tag_refs=tag_refs,
                                use_guest_agent_interface_name=use_guest_agent_interface_name,
                                ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
                                primary_ip_preference=primary_ip_preference,
                                now=now,
                                device=device,
                                overwrite_flags=overwrite_flags,
                                dns_name=vm_dns_name,
                                create_ip=assign_vm_interface_ips,
                                sync_mac=sync_vm_interface_macs,
                                vm_interface_sync_strategy=normalized_interface_strategy,
                            )
                        )

                total_interface_count = len(interface_kwargs)

                async def _create_interface_with_retry(kwargs: dict, attempts: int = 2):
                    """Create one VM interface, retrying transient failures.

                    NetBox can return transient 5xx/timeout errors when a VM has
                    many interfaces and the API is under load. Without a retry,
                    those interfaces were silently dropped. Retry a bounded
                    number of times before surfacing the failure.
                    """
                    last_error: Exception | None = None
                    for attempt in range(1, attempts + 1):
                        try:
                            return await _create_vm_interface_parallel(**kwargs)
                        except Exception as error:  # noqa: BLE001 - re-raised below
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

                # Batch interface tasks to prevent overwhelming NetBox with concurrent API calls
                from proxbox_api.routes.virtualization.virtual_machines.helpers import (
                    resolve_interface_batch_delay_ms,
                    resolve_interface_batch_size,
                )

                batch_size = resolve_interface_batch_size()
                batch_delay_ms = resolve_interface_batch_delay_ms()
                interface_results = []
                for i in range(0, len(interface_kwargs), batch_size):
                    batch = [
                        _create_interface_with_retry(kwargs)
                        for kwargs in interface_kwargs[i : i + batch_size]
                    ]
                    batch_results = await asyncio.gather(*batch, return_exceptions=True)
                    interface_results.extend(batch_results)
                    if i + batch_size < len(interface_kwargs) and batch_delay_ms > 0:
                        await asyncio.sleep(batch_delay_ms / 1000.0)
                for result in interface_results:
                    if isinstance(result, Exception):
                        failed_interface_count += 1
                        error_detail = getattr(result, "detail", str(result))
                        error_msg = f"{type(result).__name__}: {error_detail}"
                        logger.warning("Interface creation failed: %s", error_msg)
                        continue
                    if result.get("interface"):
                        netbox_vm_interfaces.append(result["interface"])
                    for ip_entry in result.get("all_ips") or []:
                        ip_id = ip_entry.get("id")
                        addr = ip_entry.get("address") or ""
                        if ip_id:
                            if ":" in addr and first_ipv6_id is None:
                                first_ipv6_id = ip_id
                            elif ":" not in addr and first_ipv4_id is None:
                                first_ipv4_id = ip_id

                if failed_interface_count:
                    logger.error(
                        "VM %s (vmid=%s): %d of %d interface(s) failed to sync; "
                        "the synchronization is degraded, not complete.",
                        resource.get("name"),
                        resource.get("vmid"),
                        failed_interface_count,
                        total_interface_count,
                    )

                core_interface_id_by_mac: dict[str, int] = {}
                ip_ids_by_interface_id: dict[int, dict[str, int]] = {}
                for result in interface_results:
                    if isinstance(result, Exception):
                        continue
                    interface_id = result.get("interface_id")
                    mac_address = result.get("mac_address")
                    if interface_id is not None and mac_address:
                        try:
                            core_interface_id_by_mac[str(mac_address)] = int(interface_id)
                        except (TypeError, ValueError):
                            pass
                    if interface_id is None:
                        continue
                    try:
                        interface_id_int = int(interface_id)
                    except (TypeError, ValueError):
                        continue
                    ip_map = ip_ids_by_interface_id.setdefault(interface_id_int, {})
                    for ip_entry in result.get("all_ips") or []:
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

                await reconcile_guest_vm_interfaces(
                    nb,
                    int(virtual_machine["id"]),
                    guest_agent_interfaces,
                    core_interface_id_by_mac,
                    ip_ids_by_interface_id,
                    tag_refs,
                    normalized_interface_strategy,
                )

            disk_tasks = [
                _create_vm_disk_parallel(
                    nb=nb,
                    virtual_machine=virtual_machine,
                    disk_entry=disk_entry,
                    cluster_name=cluster_name,
                    storage_index=storage_index,
                    tag_refs=tag_refs,
                    now=now,
                )
                for disk_entry in vm_config_obj.disks
            ]
            if disk_tasks:
                await asyncio.gather(*disk_tasks, return_exceptions=True)

        # Set primary IPs per-family. set_primary_ip preserves an already-set
        # primary for each family independently, so both IPv4 and IPv6 are tried.
        from proxbox_api.services.sync.vm_network import set_primary_ip

        any_ip_found = first_ipv4_id is not None or first_ipv6_id is not None
        if any_ip_found:
            for primary_ip_id in (first_ipv4_id, first_ipv6_id):
                if primary_ip_id is None:
                    continue
                primary_set = await set_primary_ip(
                    nb=nb,
                    virtual_machine=virtual_machine,
                    primary_ip_id=primary_ip_id,
                    primary_ip_preference=primary_ip_preference,
                )
                if not primary_set and websocket:
                    await websocket.send_json(
                        {
                            "object": "virtual_machine",
                            "data": {
                                "error": "Could not set primary IP.",
                                "rowid": virtual_machine.get("name"),
                            },
                        }
                    )
        else:
            logger.info(
                "No IP available for VM %s (vmid=%s), skipping primary IP assignment.",
                resource.get("name"),
                resource.get("vmid"),
            )
            if websocket:
                await websocket.send_json(
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

        try:
            task_history_count = await sync_virtual_machine_task_history(
                netbox_session=nb,
                pxs=pxs,
                cluster_status=cluster_status,
                virtual_machine_id=int(virtual_machine.get("id")),
                proxmox_vmid=int(resource.get("vmid")),
                vm_type=str(vm_type or "unknown"),
                cluster_name=cluster_name,
                tag_refs=tag_refs,
                websocket=websocket,
                use_websocket=use_websocket,
            )
            logger.debug(
                "Synced %s task history records for VM %s",
                task_history_count,
                resource.get("name"),
            )
        except Exception as error:
            logger.warning(
                "Error syncing task history for VM %s (%s): %s",
                resource.get("name"),
                resource.get("vmid"),
                error,
            )

        if bridge:
            if failed_interface_count:
                emit_status = "warning"
                emit_message = (
                    f"Synced VM '{vm_name}' with degraded interfaces: "
                    f"{failed_interface_count} of {total_interface_count} "
                    "interface(s) failed"
                )
            else:
                emit_status = "completed"
                emit_message = f"Synced VM '{vm_name}'"
            await bridge.emit_item_progress(
                phase="virtual-machines",
                item={
                    "name": vm_name,
                    "type": str(resource.get("type") or "unknown"),
                    "cluster": str(cluster_name),
                    "node": str(resource.get("node") or ""),
                    "netbox_id": virtual_machine.get("id"),
                    "netbox_url": virtual_machine.get("display_url"),
                    "failed_interfaces": failed_interface_count,
                    "total_interfaces": total_interface_count,
                },
                operation=ItemOperation.CREATED,
                status=emit_status,
                message=emit_message,
                progress_current=0,
                progress_total=0,
                timing_key=timing_key,
            )
            bridge.clear_timer(timing_key)

        return virtual_machine

    max_concurrency = resolve_vm_sync_concurrency()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_vm_task(cluster_name: str, resource: dict):
        async with semaphore:
            return await create_vm_task(cluster_name, resource)

    async def _create_cluster_vms(cluster: dict) -> list:
        """
        Create virtual machines for a cluster.

        Args:
            cluster: A dictionary containing cluster information.

        Returns:
            A list of virtual machine creation results.
        """

        tasks = []  # Collect coroutines
        for cluster_name, resources in cluster.items():
            for resource in resources:
                # Sync-mode filtering already applied at the source via
                # _filter_cluster_resources_by_sync_modes.
                if resource.get("type") in ("qemu", "lxc"):
                    tasks.append(_run_vm_task(cluster_name, resource))

        return await asyncio.gather(*tasks, return_exceptions=True)  # Gather coroutines

    try:
        total_vms = 0
        # Count VMs for logging
        for cluster in filtered_cluster_resources:
            cluster_name = list(cluster.keys())[0]
            resources = cluster[cluster_name]
            vm_count = len([r for r in resources if r.get("type") in ("qemu", "lxc")])
            total_vms += vm_count

        # Return the created virtual machines.
        result_list = await asyncio.gather(
            *[_create_cluster_vms(cluster) for cluster in filtered_cluster_resources],
            return_exceptions=True,
        )

        logger.debug(
            "VM creation gather complete: %d cluster result(s)",
            len(result_list),
        )
        for cluster_result in result_list:
            if isinstance(cluster_result, Exception):
                continue
            for result in cluster_result:
                if isinstance(result, Exception):
                    logger.warning(
                        "VM sub-task failed: %s",
                        getattr(result, "python_exception", str(result)),
                    )

        # Flatten the nested results and process them
        for cluster_results in result_list:
            if isinstance(cluster_results, Exception):
                failed_vms += 1
            else:
                # cluster_results is a list of VM creation results
                for vm_result in cluster_results:
                    if isinstance(vm_result, Exception):
                        failed_vms += 1
                    else:
                        successful_vms += 1
                        flattened_results.append(vm_result)

        if bridge:
            await bridge.emit_phase_summary(
                phase="virtual-machines",
                created=successful_vms,
                failed=failed_vms,
                message=(
                    f"Virtual machine sync completed: {successful_vms} synchronized, {failed_vms} failed"
                ),
            )

        # Send end message to websocket
        if all([use_websocket, websocket]):
            await websocket.send_json({"object": "virtual_machine", "end": True})

        # Clear cache after creating virtual machines
        global_cache.clear_cache()

        logger.info(
            "VM sync summary: total=%s ok=%s failed=%s",
            total_vms,
            successful_vms,
            failed_vms,
        )

    except Exception as error:
        error_msg = f"Error during VM sync: {str(error)}"
        if bridge:
            await bridge.emit_error_detail(
                message="Virtual machine sync failed",
                category=ErrorCategory.INTERNAL,
                phase="virtual-machines",
                detail=str(error),
                suggestion="Review backend logs and retry the synchronization",
            )
        raise ProxboxException(message=error_msg)

    return flattened_results


async def create_only_vm_interfaces(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
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
        custom_fields: Custom field configs.
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
    tag_refs = [t for t in tag_refs if t.get("name") and t.get("slug")]
    now = datetime.now(timezone.utc)
    results: list[dict] = []
    sync_warnings: list[dict[str, object]] = []
    normalized_interface_strategy = normalize_vm_interface_sync_strategy(vm_interface_sync_strategy)
    if normalized_interface_strategy == "legacy_rename":
        warn_legacy_vm_interface_strategy()

    vm_snapshot = await _load_netbox_virtual_machine_snapshot(nb)
    vm_index = _build_vm_index_by_proxmox_id(vm_snapshot)
    vm_candidates_by_vmid = _build_vm_candidates_by_proxmox_id(vm_snapshot)
    cluster_id_cache = _build_cluster_id_cache_from_vm_snapshot(vm_snapshot)
    endpoint_id_by_cluster = _endpoint_id_by_cluster_names(pxs, cluster_status)

    async def _sync_vm_interfaces(  # noqa: C901
        cluster_name: str,
        cluster_id: int | None,
        endpoint_id: int | None,
        resource: dict,
    ) -> tuple[list[dict], dict]:
        """Collect interface payloads for a single VM. Returns (payloads, interface_info_dict)."""
        cluster_name_str = str(cluster_name)
        resource_node = str(resource.get("node", ""))
        vm_type = resource.get("type", "unknown")
        vm_name = str(resource.get("name", "")).strip()

        vm_record = None
        for cluster in cluster_resources:
            if not isinstance(cluster, dict):
                continue
            for c_name, c_resources in cluster.items():
                if str(c_name) != cluster_name_str:
                    continue
                for r in c_resources:
                    if r.get("name", "").strip() == vm_name and r.get("type") == vm_type:
                        vm_record = r
                        break

        if not vm_record:
            return [], {}

        vmid = resource.get("vmid")
        if vmid is None:
            return [], {}

        netbox_vm = _resolve_vm_from_index_or_unique_vmid(
            vm_index,
            vm_candidates_by_vmid,
            endpoint_id=endpoint_id,
            raw_vmid=vmid,
            cluster_name=cluster_name_str,
            sync_context="interface",
        )
        if not netbox_vm:
            logger.warning(
                "Skipping VM interface sync for %s (cluster=%s cluster_id=%s vmid=%s): "
                "NetBox VM not found",
                vm_name,
                cluster_name_str,
                cluster_id,
                vmid,
            )
            return [], {}

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
            logger.warning("Could not fetch VM config for %s (vmid=%s): %s", vm_name, vmid, exc)

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

        vm_networks = _parse_vm_networks(vm_config)
        interface_payloads: list[dict] = []
        interface_info: dict = {}

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

                if use_websocket and websocket:
                    await websocket.send_json(
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

                try:
                    iface_mac = config_dict.get("virtio") or config_dict.get("hwaddr")
                    # Collect interface payload info for later bulk processing
                    payload = {
                        "name": resolved_name,
                        "enabled": True,
                        "bridge": None,
                        "untagged_vlan": None,
                        "mode": None,
                        "tags": tag_refs,
                        "custom_fields": {"proxmox_last_updated": now.isoformat()},
                        "virtual_machine": netbox_vm.get("id"),
                    }

                    # Store bridge reference info for later resolution
                    vlan_tag = config_dict.get("tag")
                    bridge_name = config_dict.get("bridge")

                    # Store metadata for processing
                    key = f"{netbox_vm.get('id')}:{resolved_name}"
                    interface_info[key] = {
                        "payload": payload,
                        "mac_address": iface_mac,
                        "vlan_tag": vlan_tag,
                        "bridge_name": bridge_name,
                        "vm_id": netbox_vm.get("id"),
                        "resource_node": resource_node,
                        "resolved_name": resolved_name,
                        "config_dict": config_dict,
                        "guest_iface": guest_iface,
                        "guest_agent_interfaces": guest_agent_interfaces,
                        "vm_name": vm_name,
                        "site_id": _relation_id(netbox_vm.get("site")),
                        "tenant_id": _relation_id(netbox_vm.get("tenant")),
                    }
                    interface_payloads.append(payload)
                except Exception as exc:
                    error_detail = getattr(exc, "detail", str(exc))
                    error_msg = f"{type(exc).__name__}: {error_detail}"
                    logger.warning(
                        "Failed to collect interface payload %s for VM %s: %s",
                        resolved_name,
                        vm_name,
                        error_msg,
                    )
                    if use_websocket and websocket:
                        await websocket.send_json(
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

        return interface_payloads, interface_info

    max_concurrency = resolve_vm_sync_concurrency()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_task(
        cluster_name: str,
        cluster_id: int | None,
        endpoint_id: int | None,
        resource: dict,
    ) -> tuple[list[dict], dict]:
        async with semaphore:
            return await _sync_vm_interfaces(cluster_name, cluster_id, endpoint_id, resource)

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

    # Collect all interface payloads and metadata from all VMs
    all_interface_payloads: list[dict] = []
    all_interface_info: dict = {}
    all_vlan_tags: dict[tuple[int, int | None, int | None], list[dict]] = {}

    try:
        for cluster in cluster_resources:
            cluster_results = await _create_cluster_tasks(cluster)
            for cluster_result in cluster_results:
                if isinstance(cluster_result, Exception):
                    continue
                payloads, iface_info = cluster_result
                if isinstance(payloads, list):
                    all_interface_payloads.extend(payloads)
                    all_interface_info.update(iface_info)

                    # Collect VLAN tags for bulk creation
                    for key, info in iface_info.items():
                        vlan_tag = info.get("vlan_tag")
                        if vlan_tag:
                            try:
                                vid = int(vlan_tag)
                                site_id = info.get("site_id")
                                tenant_id = info.get("tenant_id")
                                vlan_key = (
                                    vid,
                                    site_id if isinstance(site_id, int) else None,
                                    tenant_id if isinstance(tenant_id, int) else None,
                                )
                                if vlan_key not in all_vlan_tags:
                                    all_vlan_tags[vlan_key] = []
                            except (ValueError, TypeError):
                                pass
    except Exception as exc:
        error_detail = getattr(exc, "detail", str(exc))
        error_msg = f"{type(exc).__name__}: {error_detail}"
        logger.warning("Error during VM interfaces collection: %s", error_msg)

    # Bulk reconcile VLANs first
    vlan_vid_to_id = {}
    if all_vlan_tags:
        try:
            from proxbox_api.services.sync.network import (
                build_vlan_payload,
                bulk_reconcile_vlans,
            )

            vlan_payloads = [
                build_vlan_payload(
                    vid,
                    tag_refs,
                    now,
                    site_id=site_id,
                    tenant_id=tenant_id,
                )
                for vid, site_id, tenant_id in all_vlan_tags.keys()
            ]
            vlan_vid_to_id = await bulk_reconcile_vlans(nb, vlan_payloads)
            logger.info(
                "Bulk VLAN reconciliation completed: %d VLANs processed", len(vlan_payloads)
            )
        except Exception as e:
            logger.error("Error during VLAN bulk reconciliation: %s", e)

    # Update interface payloads with resolved VLAN IDs
    for key, info in all_interface_info.items():
        vlan_tag = info.get("vlan_tag")
        if vlan_tag:
            try:
                vid = int(vlan_tag)
                site_id = info.get("site_id")
                tenant_id = info.get("tenant_id")
                vlan_key = (
                    vid,
                    site_id if isinstance(site_id, int) else None,
                    tenant_id if isinstance(tenant_id, int) else None,
                )
                vlan_id = vlan_vid_to_id.get(vlan_key) or vlan_vid_to_id.get(vid)
                if vlan_id is not None:
                    info["payload"]["untagged_vlan"] = vlan_id
                    info["payload"]["mode"] = "access"
            except (ValueError, TypeError):
                pass

    # Bulk reconcile interfaces
    results = []
    if all_interface_payloads:
        try:
            from proxbox_api.services.sync.network import bulk_reconcile_vm_interfaces

            created_interfaces, interface_name_vm_to_id = await bulk_reconcile_vm_interfaces(
                nb, all_interface_payloads, overwrite_flags=overwrite_flags
            )
            successful_interface_count = len(created_interfaces)
            failed_interface_count = max(
                len(all_interface_payloads) - successful_interface_count,
                0,
            )
            logger.info(
                "Bulk interface reconciliation completed: %d interfaces processed",
                len(all_interface_payloads),
            )
            if failed_interface_count:
                warning_message = (
                    "VM interface reconciliation completed with partial failures: "
                    f"{successful_interface_count} succeeded, {failed_interface_count} failed."
                )
                warning_payload = {
                    "phase": "vm-interfaces",
                    "succeeded": successful_interface_count,
                    "failed": failed_interface_count,
                    "requested": len(all_interface_payloads),
                    "message": warning_message,
                }
                sync_warnings.append(warning_payload)
                logger.warning(warning_message)
                if use_websocket and websocket and hasattr(websocket, "emit_phase_summary"):
                    await websocket.emit_phase_summary(
                        phase="vm-interfaces",
                        created=successful_interface_count,
                        failed=failed_interface_count,
                        message=warning_message,
                    )

            # Per-interface MAC reconcile: NetBox 4.2+ stores MACs as a separate
            # dcim.MACAddress row referenced by VMInterface.primary_mac_address.
            from proxbox_api.services.sync.mac_address import (
                reconcile_mac_for_vm_interface,
            )

            if sync_mac:
                for key, info in all_interface_info.items():
                    mac_value = info.get("mac_address")
                    if not mac_value:
                        continue
                    vm_id_for_mac = info.get("vm_id")
                    iface_name_for_mac = info.get("resolved_name")
                    if not vm_id_for_mac or not iface_name_for_mac:
                        continue
                    iface_id_for_mac = interface_name_vm_to_id.get(
                        (iface_name_for_mac, int(vm_id_for_mac))
                    )
                    if not iface_id_for_mac:
                        continue
                    try:
                        await reconcile_mac_for_vm_interface(
                            nb,
                            vminterface_id=int(iface_id_for_mac),
                            mac=mac_value,
                            tag_refs=tag_refs,
                        )
                    except Exception as mac_exc:
                        logger.warning(
                            "Failed to reconcile MAC %s for VM interface %s: %s",
                            mac_value,
                            iface_id_for_mac,
                            mac_exc,
                        )

            guest_contexts: dict[int, dict[str, object]] = {}
            for info in all_interface_info.values():
                vm_id_for_guest = info.get("vm_id")
                iface_name_for_guest = info.get("resolved_name")
                if not vm_id_for_guest or not iface_name_for_guest:
                    continue
                try:
                    vm_id_int = int(vm_id_for_guest)
                except (TypeError, ValueError):
                    continue
                iface_id = interface_name_vm_to_id.get((iface_name_for_guest, vm_id_int))
                context = guest_contexts.setdefault(
                    vm_id_int,
                    {
                        "guest_interfaces": info.get("guest_agent_interfaces") or [],
                        "core_interface_id_by_mac": {},
                    },
                )
                if not context.get("guest_interfaces"):
                    context["guest_interfaces"] = info.get("guest_agent_interfaces") or []
                mac_value = info.get("mac_address")
                if mac_value and iface_id:
                    cast("dict[str, int]", context["core_interface_id_by_mac"])[str(mac_value)] = (
                        int(iface_id)
                    )

            for vm_id_int, context in guest_contexts.items():
                await reconcile_guest_vm_interfaces(
                    nb,
                    vm_id_int,
                    cast("list[dict[str, object]]", context.get("guest_interfaces") or []),
                    cast("dict[str, int]", context.get("core_interface_id_by_mac") or {}),
                    {},
                    tag_refs,
                    normalized_interface_strategy,
                )

            # Emit WebSocket progress for each created interface
            if use_websocket and websocket:
                for interface in created_interfaces:
                    # Find the original info for this interface
                    iface_name = interface.get("name")
                    vm_id = interface.get("virtual_machine")
                    iface_id = interface.get("id")

                    key = f"{vm_id}:{iface_name}"
                    if key in all_interface_info:
                        info = all_interface_info[key]
                        await websocket.send_json(
                            {
                                "object": "vm_interface",
                                "data": {
                                    "completed": True,
                                    "rowid": iface_name,
                                    "name": iface_name,
                                    "vm": info.get("vm_name"),
                                    "netbox_id": iface_id,
                                    "mac_address": interface.get("mac_address"),
                                },
                            }
                        )

            # Build results list for compatibility
            results = [
                {
                    "id": i.get("id"),
                    "mac_address": i.get("mac_address"),
                    "interface": i,
                }
                for i in created_interfaces
            ]
        except Exception as e:
            # Surface bulk failures to the stream consumer instead of returning
            # a silent empty success (interfaces simply "missing" in NetBox).
            error_detail = f"{type(e).__name__}: {getattr(e, 'detail', str(e))}"
            logger.error("Error during interface bulk reconciliation: %s", error_detail)
            if use_websocket and websocket:
                await websocket.send_json(
                    {
                        "object": "vm_interface",
                        "data": {
                            "completed": False,
                            "sync_status": "failed",
                            "error": error_detail,
                        },
                    }
                )
                await websocket.send_json({"object": "vm_interface", "end": True})
            raise ProxboxException(
                message=(
                    "VM interface bulk reconciliation failed; interface sync is "
                    "incomplete for this pass."
                ),
                python_exception=error_detail,
            ) from e

    # Create node-level dcim bridge interfaces for any NIC that references a
    # Proxmox bridge (e.g. vmbr0, vmbr1).  The bulk path skips bridge resolution
    # during payload collection, so we handle it here after all VM interfaces exist.
    # Then update each NIC's proxbox_bridge custom field with the dcim.Interface ID.
    if all_interface_info:
        from proxbox_api.netbox_rest import rest_first_async
        from proxbox_api.services.sync.bridge_interfaces import ensure_bridge_interfaces

        node_device_id_cache: dict[str, int | None] = {}

        async def _resolve_device_id(node_name: str) -> int | None:
            if node_name in node_device_id_cache:
                return node_device_id_cache[node_name]
            try:
                device_record = await rest_first_async(
                    nb,
                    "/api/dcim/devices/",
                    query={"name": node_name, "limit": 1},
                )
                did = (
                    device_record.get("id")
                    if isinstance(device_record, dict)
                    else getattr(device_record, "id", None)
                )
            except Exception:
                did = None
            node_device_id_cache[node_name] = did
            return did

        for key, info in all_interface_info.items():
            bridge_name = info.get("bridge_name")
            if not bridge_name:
                continue
            vm_id_val = info.get("vm_id")
            resource_node_val = info.get("resource_node", "")
            if not vm_id_val:
                continue
            try:
                device_id_val = (
                    await _resolve_device_id(resource_node_val) if resource_node_val else None
                )
                vm_bridge_id = await ensure_bridge_interfaces(
                    nb,
                    device_id_val,
                    int(vm_id_val),
                    bridge_name,
                    tag_refs,
                    now,
                    overwrite_flags=overwrite_flags,
                )
                # Update the NIC interface in NetBox to set the bridge FK.
                if vm_bridge_id:
                    resolved_name = info.get("resolved_name", "")
                    if resolved_name:
                        try:
                            existing_iface = await rest_first_async(
                                nb,
                                "/api/virtualization/interfaces/",
                                query={
                                    "virtual_machine_id": int(vm_id_val),
                                    "name": resolved_name,
                                    "limit": 1,
                                },
                            )
                            if existing_iface:
                                iface_id = (
                                    existing_iface.get("id")
                                    if isinstance(existing_iface, dict)
                                    else getattr(existing_iface, "id", None)
                                )
                                overwrite_bridge_custom_fields = (
                                    overwrite_flags is None
                                    or overwrite_flags.overwrite_vm_interface_custom_fields
                                )
                                if iface_id and include_custom_fields_in_payload(
                                    overwrite_bridge_custom_fields,
                                    context="legacy VM-interface bridge custom-field patch",
                                ):
                                    try:
                                        await rest_patch_async(
                                            nb,
                                            "/api/virtualization/interfaces/",
                                            iface_id,
                                            {"custom_fields": {"proxbox_bridge": vm_bridge_id}},
                                        )
                                    except Exception as patch_exc:
                                        logger.warning(
                                            "Failed to set proxbox_bridge on interface %s "
                                            "(VM %s): %s",
                                            resolved_name,
                                            vm_id_val,
                                            patch_exc,
                                        )
                                    await write_vm_interface_sync_state(
                                        nb,
                                        vm_interface_id=iface_id,
                                        proxbox_bridge_id=vm_bridge_id,
                                        overwrite_custom_fields=overwrite_bridge_custom_fields,
                                    )
                                elif iface_id:
                                    await write_vm_interface_sync_state(
                                        nb,
                                        vm_interface_id=iface_id,
                                        proxbox_bridge_id=vm_bridge_id,
                                        overwrite_custom_fields=overwrite_bridge_custom_fields,
                                    )
                        except Exception as lookup_exc:
                            logger.warning(
                                "Failed to resolve interface %s (VM %s) for proxbox_bridge: %s",
                                resolved_name,
                                vm_id_val,
                                lookup_exc,
                            )
            except Exception as bridge_exc:
                logger.warning(
                    "Failed to create bridge interfaces for %s on VM %s: %s",
                    bridge_name,
                    vm_id_val,
                    bridge_exc,
                )

    if use_websocket and websocket:
        await websocket.send_json({"object": "vm_interface", "end": True})

    return SyncResultList(results, warnings=sync_warnings)


async def create_only_vm_ip_addresses(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
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
        custom_fields: Custom field configs.
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
    normalized_interface_strategy = normalize_vm_interface_sync_strategy(vm_interface_sync_strategy)
    if normalized_interface_strategy == "legacy_rename":
        warn_legacy_vm_interface_strategy()

    vm_snapshot = await _load_netbox_virtual_machine_snapshot(nb)
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

    return results


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
    custom_fields: CreateCustomFieldsDep,
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
    run_id: str | None = Query(
        default=None,
        title="Run ID",
        description=(
            "UUID stamped on each touched VM's proxbox_last_run_id custom field. "
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
        custom_fields=custom_fields,
        tag=tag,
        use_guest_agent_interface_name=use_guest_agent_interface_name,
        vm_interface_sync_strategy=vm_interface_sync_strategy,
        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
        primary_ip_preference=primary_ip_preference,
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
    custom_fields: CreateCustomFieldsDep,
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
        title="Overwrite VM Custom Fields",
        description=(
            "When false, custom_fields are not patched on existing VMs that already have "
            "non-empty custom_fields. Custom fields are still applied on first create. "
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
    run_id: str | None = Query(
        default=None,
        title="Run ID",
        description=(
            "UUID stamped on each touched VM's proxbox_last_run_id custom field. "
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
    vm_ids: list[int] = []

    if netbox_vm_ids:
        vm_ids = parse_comma_separated_ints(netbox_vm_ids)
        if vm_ids:
            filtered_cluster_resources = await _filter_cluster_resources_by_netbox_vm_ids(
                netbox_session=netbox_session,
                cluster_resources=cluster_resources,
                netbox_vm_ids=vm_ids,
            )

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await create_virtual_machines(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=filtered_cluster_resources,
                    custom_fields=custom_fields,
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
            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "completed",
                    "message": "Virtual machines synchronization finished.",
                    "result": {"count": len(result)},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Virtual machines sync completed.",
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
    custom_fields: CreateCustomFieldsDep,
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
        title="Overwrite VM Custom Fields",
        description=(
            "When false, custom_fields are not patched on existing VMs that already have "
            "non-empty custom_fields. Custom fields are still applied on first create. "
            "When unset, falls back to overwrite_flags.overwrite_vm_custom_fields."
        ),
    ),
    run_id: str | None = Query(
        default=None,
        title="Run ID",
        description=(
            "UUID stamped on each touched VM's proxbox_last_run_id custom field. "
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
                    custom_fields=custom_fields,
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
