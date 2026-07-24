"""Virtual disks synchronization service from Proxmox to NetBox."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    RestRecord,
    rest_bulk_delete_async,
    rest_bulk_reconcile_async,
    rest_list_async,
    rest_list_paginated_async,
    rest_patch_async,
)
from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualDiskSyncState, ProxmoxVmConfigInput
from proxbox_api.runtime_settings import get_int
from proxbox_api.services.custom_fields import legacy_custom_fields_payload
from proxbox_api.services.proxmox.config import resolve_vm_config
from proxbox_api.services.sync.storage_links import (
    build_storage_index,
    find_storage_record,
    storage_name_from_volume_id,
)
from proxbox_api.services.sync.sync_state_writer import write_virtual_disk_sync_state
from proxbox_api.services.sync.vm_helpers import (
    list_netbox_virtual_machines_by_ids,
    relation_id,
    relation_name,
    require_selected_netbox_vm_coverage,
    to_mapping,
)
from proxbox_api.services.sync.vmid_helpers import (
    extract_proxmox_endpoint_id,
    extract_proxmox_vm_type,
    extract_proxmox_vmid,
    normalize_vmid,
    select_proxmox_sessions_by_endpoint,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils import return_status_html


@dataclass(frozen=True)
class VmConfigTarget:
    node: str | None
    vm_type: str
    cluster_name: str | None
    endpoint_id: int | None
    source: str
    netbox_device_name: str | None
    custom_field_node: str | None


@dataclass(frozen=True)
class VmDiskFetchResult:
    vm: dict[str, object]
    vmid: str
    vm_name: str
    cluster_name: str | None
    target: VmConfigTarget | None
    vm_config: dict[str, object] | None
    failure_message: str | None = None


@dataclass(frozen=True)
class VmDiskSyncOutcome:
    state: str
    disks_created: int = 0
    disks_updated: int = 0
    disks_deleted: int = 0
    parent_vm_disk_updated: bool = False


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _proxmox_node_custom_field(vm: dict[str, object]) -> str | None:
    vm_data = to_mapping(vm)
    for key in ("proxmox_node", "cf_proxmox_node"):
        value = _text_or_none(vm_data.get(key))
        if value:
            return value
    custom_fields = vm_data.get("custom_fields")
    if isinstance(custom_fields, dict):
        for key in ("proxmox_node", "cf_proxmox_node"):
            value = _text_or_none(custom_fields.get(key))
            if value:
                return value
    return None


def _iter_cluster_vm_resources(
    cluster_resources: list[dict[str, object]] | None,
    *,
    vmid: str,
):
    for cluster_entry in cluster_resources or []:
        if not isinstance(cluster_entry, dict):
            continue
        for cluster_key, resources in cluster_entry.items():
            if not isinstance(resources, list):
                continue
            cluster_key_text = _text_or_none(cluster_key)
            for resource in resources:
                if isinstance(resource, dict) and normalize_vmid(resource.get("vmid")) == vmid:
                    yield cluster_key_text, resource


def _cluster_resource_target(
    cluster_resources: list[dict[str, object]] | None,
    *,
    vmid: str,
    vm_type: str,
    cluster_name: str | None,
    allow_type_override: bool,
) -> tuple[str | None, str, str] | None:
    candidates: list[tuple[str | None, str, str]] = []
    for cluster_key_text, resource in _iter_cluster_vm_resources(cluster_resources, vmid=vmid):
        resource_type = (_text_or_none(resource.get("type")) or "").lower()
        if resource_type not in ("qemu", "lxc"):
            resource_type = ""
        if resource_type and resource_type != vm_type and not allow_type_override:
            continue
        node = _text_or_none(resource.get("node"))
        if node:
            candidates.append((cluster_key_text, node, resource_type or vm_type))

    if not candidates:
        return None
    if cluster_name:
        for candidate in candidates:
            if candidate[0] == cluster_name:
                return candidate
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolve_vm_config_target(
    *,
    vm: dict[str, object],
    vmid: str,
    vm_type: str,
    vm_type_was_explicit: bool,
    cluster_name: str | None,
    cluster_resources: list[dict[str, object]] | None,
) -> VmConfigTarget:
    vm_data = to_mapping(vm)
    device_name = relation_name(vm_data.get("device"))
    custom_field_node = _proxmox_node_custom_field(vm_data)
    endpoint_id = extract_proxmox_endpoint_id(vm_data)

    cluster_target = _cluster_resource_target(
        cluster_resources,
        vmid=vmid,
        vm_type=vm_type,
        cluster_name=cluster_name,
        allow_type_override=not vm_type_was_explicit,
    )
    if cluster_target:
        target_cluster, node, target_vm_type = cluster_target
        return VmConfigTarget(
            node=node,
            vm_type=target_vm_type,
            cluster_name=target_cluster or cluster_name,
            endpoint_id=endpoint_id,
            source="cluster_resources",
            netbox_device_name=device_name,
            custom_field_node=custom_field_node,
        )

    if custom_field_node:
        return VmConfigTarget(
            node=custom_field_node,
            vm_type=vm_type,
            cluster_name=cluster_name,
            endpoint_id=endpoint_id,
            source="custom_fields.proxmox_node",
            netbox_device_name=device_name,
            custom_field_node=custom_field_node,
        )

    return VmConfigTarget(
        node=device_name,
        vm_type=vm_type,
        cluster_name=cluster_name,
        endpoint_id=endpoint_id,
        source="device.name" if device_name else "unresolved",
        netbox_device_name=device_name,
        custom_field_node=custom_field_node,
    )


async def _list_all_vms_with_proxmox_id(
    nb,
    batch_size: int = 500,
) -> list[RestRecord]:
    """List all VMs from NetBox with pagination handling."""
    return await rest_list_paginated_async(
        nb,
        "/api/virtualization/virtual-machines/",
        page_size=batch_size,
    )


async def _delete_stale_virtual_disks(
    nb: object,
    *,
    vm_id: int,
    desired_disks: dict[str, int],
) -> int:
    """Delete VM disk records missing from Proxmox, including duplicate names."""

    existing_disks = await rest_list_async(
        nb,
        "/api/virtualization/virtual-disks/",
        query={"virtual_machine_id": vm_id, "limit": 500},
    )
    stale_ids: list[int] = []
    desired_names = set(desired_disks)
    desired_records: dict[str, list[tuple[int, int]]] = {}
    for record in existing_disks:
        data = to_mapping(record)
        name = str(data.get("name") or "").strip()
        record_id = relation_id(data.get("id"))
        if not name or record_id is None:
            continue
        if name not in desired_names:
            stale_ids.append(record_id)
            continue
        try:
            size = int(data.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        desired_records.setdefault(name, []).append((record_id, size))

    for name, records in desired_records.items():
        if len(records) <= 1:
            continue
        desired_size = desired_disks[name]
        keep_id = next(
            (record_id for record_id, size in records if size == desired_size),
            records[0][0],
        )
        stale_ids.extend(record_id for record_id, _size in records if record_id != keep_id)

    if not stale_ids:
        return 0

    stale_ids = sorted(set(stale_ids))
    deleted = await rest_bulk_delete_async(nb, "/api/virtualization/virtual-disks/", stale_ids)
    logger.info(
        "Deleted %s stale virtual disk(s) for VM id=%s: %s",
        deleted,
        vm_id,
        sorted(stale_ids),
    )
    return deleted


async def _sync_parent_vm_disk_total(
    nb: object,
    *,
    vm: dict[str, object],
    desired_disk_total: int,
) -> bool:
    """Patch the VM disk total after child virtual disks have converged."""
    # vm may arrive as a RestRecord (no __contains__ / __setitem__); normalise to dict.
    vm_data = to_mapping(vm)

    if "disk" not in vm_data:
        return False

    vm_id = relation_id(vm_data.get("id"))
    if vm_id is None:
        return False

    try:
        current_disk = int(vm_data.get("disk") or 0)
    except (TypeError, ValueError):
        current_disk = 0
    if current_disk == desired_disk_total:
        return False

    await rest_patch_async(
        nb,
        "/api/virtualization/virtual-machines/",
        vm_id,
        {"disk": desired_disk_total},
    )
    vm_data["disk"] = desired_disk_total
    return True


def _resolve_fetch_concurrency(fetch_max_concurrency: int | None) -> int:
    if fetch_max_concurrency is not None:
        return max(1, int(fetch_max_concurrency))
    return get_int(
        settings_key="vm_sync_max_concurrency",
        env="PROXBOX_VM_SYNC_MAX_CONCURRENCY",
        default=4,
        minimum=1,
    )


def _resolve_write_concurrency() -> int:
    return get_int(
        settings_key="netbox_write_concurrency",
        env="PROXBOX_NETBOX_WRITE_CONCURRENCY",
        default=8,
        minimum=1,
    )


def _resolve_cluster_name(
    vm: dict[str, object],
    cluster_status: list[object] | None,
) -> str | None:
    cluster = vm.get("cluster")
    if isinstance(cluster, dict):
        cluster_name = _text_or_none(cluster.get("name"))
        if cluster_name:
            return cluster_name

    for cs in cluster_status or []:
        cs_name = _text_or_none(getattr(cs, "name", None))
        if cs_name:
            return cs_name
    return None


async def _send_virtual_disk_payload(
    *,
    websocket: object | None,
    websocket_lock: asyncio.Lock | None,
    use_websocket: bool,
    payload: dict[str, object],
) -> None:
    if not use_websocket or websocket is None:
        return
    if websocket_lock is None:
        await websocket.send_json(payload)
        return
    async with websocket_lock:
        await websocket.send_json(payload)


def _virtual_disk_rowid(vm_name: str) -> str:
    return f"{vm_name}-disks"


async def _fetch_virtual_disk_vm_config(
    *,
    vm: dict[str, object],
    pxs: ProxmoxSessionsDep,
    cluster_status: list[object] | None,
    cluster_resources: list[dict[str, object]] | None,
) -> VmDiskFetchResult:
    vmid = extract_proxmox_vmid(vm)
    vm_name = str(vm.get("name", "unknown") or "unknown")
    if not vmid:
        return VmDiskFetchResult(
            vm=vm,
            vmid="",
            vm_name=vm_name,
            cluster_name=_resolve_cluster_name(vm, cluster_status),
            target=None,
            vm_config=None,
            failure_message="Missing proxmox VM id",
        )

    cluster_name = _resolve_cluster_name(vm, cluster_status)
    extracted_vm_type = extract_proxmox_vm_type(vm)
    vm_type = extracted_vm_type or "qemu"
    target = _resolve_vm_config_target(
        vm=vm,
        vmid=vmid,
        vm_type=vm_type,
        vm_type_was_explicit=extracted_vm_type is not None,
        cluster_name=cluster_name,
        cluster_resources=cluster_resources,
    )
    node_name = target.node
    vm_type = target.vm_type
    cluster_name = target.cluster_name

    if not node_name:
        logger.warning(
            "No node found for VM %s (vmid=%s type=%s cluster=%s, device=%s custom_field_node=%s), skipping disk sync",
            vm_name,
            vmid,
            vm_type,
            cluster_name,
            target.netbox_device_name,
            target.custom_field_node,
        )
        return VmDiskFetchResult(
            vm=vm,
            vmid=vmid,
            vm_name=vm_name,
            cluster_name=cluster_name,
            target=target,
            vm_config=None,
            failure_message="No node associated",
        )

    logger.debug(
        "Resolved virtual disk VM config target for %s (vmid=%s type=%s endpoint_id=%s cluster=%s node=%s source=%s)",
        vm_name,
        vmid,
        vm_type,
        target.endpoint_id,
        cluster_name,
        node_name,
        target.source,
    )

    target_pxs = select_proxmox_sessions_by_endpoint(pxs, target.endpoint_id)
    if target.endpoint_id is not None and not target_pxs:
        logger.warning(
            "No Proxmox session found for VM %s (vmid=%s endpoint_id=%s), skipping disk sync",
            vm_name,
            vmid,
            target.endpoint_id,
        )
        return VmDiskFetchResult(
            vm=vm,
            vmid=vmid,
            vm_name=vm_name,
            cluster_name=cluster_name,
            target=target,
            vm_config=None,
            failure_message="Endpoint session not available",
        )

    try:
        vm_config = await resolve_vm_config(
            pxs=target_pxs,
            node=node_name,
            vm_type=vm_type,
            vmid=vmid,
        )
    except Exception as error:
        logger.error(
            "Error getting VM config for %s (vmid=%s type=%s cluster=%s node=%s source=%s): %s",
            vm_name,
            vmid,
            vm_type,
            cluster_name,
            node_name,
            target.source,
            error,
        )
        return VmDiskFetchResult(
            vm=vm,
            vmid=vmid,
            vm_name=vm_name,
            cluster_name=cluster_name,
            target=target,
            vm_config=None,
            failure_message="Config not available",
        )

    if not vm_config:
        logger.warning("Could not get VM config for VM %s (vmid: %s)", vm_name, vmid)
        return VmDiskFetchResult(
            vm=vm,
            vmid=vmid,
            vm_name=vm_name,
            cluster_name=cluster_name,
            target=target,
            vm_config=None,
            failure_message="Config not available",
        )

    return VmDiskFetchResult(
        vm=vm,
        vmid=vmid,
        vm_name=vm_name,
        cluster_name=cluster_name,
        target=target,
        vm_config=vm_config,
    )


async def _sync_virtual_disks_for_vm(
    *,
    nb: object,
    fetched_vm: VmDiskFetchResult,
    storage_index: dict[tuple[str, str], dict],
    tag_refs: list[dict[str, object]],
    websocket: object | None,
    websocket_lock: asyncio.Lock | None,
    use_websocket: bool,
    completed_html: str,
    failed_html: str,
) -> VmDiskSyncOutcome:
    vm = fetched_vm.vm
    vm_name = fetched_vm.vm_name
    vm_id = vm.get("id")

    try:
        vm_config_obj = await asyncio.to_thread(
            ProxmoxVmConfigInput.model_validate,
            fetched_vm.vm_config,
        )
        disk_entries = vm_config_obj.disks

        disks_created = 0
        disks_updated = 0
        disks_deleted = 0
        parent_vm_disk_updated = False

        disk_payloads: list[dict[str, object]] = []
        for disk_entry in disk_entries:
            storage_name = disk_entry.storage_name or storage_name_from_volume_id(
                disk_entry.storage
            )
            storage_record = find_storage_record(
                storage_index,
                cluster_name=fetched_vm.cluster_name,
                storage_name=storage_name,
            )
            storage_id = storage_record.get("id") if storage_record else None
            custom_fields: dict[str, object] = {}
            if storage_id is not None:
                custom_fields["proxbox_storage_id"] = storage_id
            disk_payload: dict[str, object] = {
                "virtual_machine": vm_id,
                "name": disk_entry.name,
                "size": disk_entry.size_mb,
                "storage": storage_id,
                "description": disk_entry.description,
                "tags": tag_refs,
            }
            if custom_fields:
                disk_payload["custom_fields"] = custom_fields
            disk_payloads.append(disk_payload)

        desired_disk_sizes = {
            str(payload.get("name")): int(payload.get("size") or 0) for payload in disk_payloads
        }
        desired_disk_total = sum(int(payload.get("size") or 0) for payload in disk_payloads)

        if disk_payloads:
            bulk_result = await rest_bulk_reconcile_async(
                nb,
                "/api/virtualization/virtual-disks/",
                payloads=[
                    legacy_custom_fields_payload(
                        payload,
                        overwrite=True,
                        context="legacy virtual-disk custom-field payload",
                    )
                    for payload in disk_payloads
                ],
                lookup_fields=["virtual_machine", "name"],
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
                base_query={"virtual_machine_id": vm_id},
                lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
                strict_lookup=True,
                nullable_fields={"storage"},
            )
            disks_created = bulk_result.created
            disks_updated = bulk_result.updated
            sidecar_payload_by_key = {
                (
                    relation_id(payload.get("virtual_machine")),
                    str(payload.get("name") or ""),
                ): payload
                for payload in disk_payloads
            }
            for record in bulk_result.records:
                record_vm_id = relation_id(record.get("virtual_machine"))
                sidecar_payload = sidecar_payload_by_key.get(
                    (record_vm_id, str(record.get("name") or ""))
                )
                custom_fields = (
                    sidecar_payload.get("custom_fields")
                    if isinstance(sidecar_payload, dict)
                    else None
                )
                await write_virtual_disk_sync_state(
                    nb,
                    virtual_disk_id=record.get("id"),
                    proxbox_storage_id=(
                        custom_fields.get("proxbox_storage_id")
                        if isinstance(custom_fields, dict)
                        else None
                    ),
                    overwrite_custom_fields=True,
                )

        vm_id_int = relation_id(vm_id)
        if vm_id_int is not None:
            disks_deleted = await _delete_stale_virtual_disks(
                nb,
                vm_id=vm_id_int,
                desired_disks=desired_disk_sizes,
            )
            parent_vm_disk_updated = await _sync_parent_vm_disk_total(
                nb,
                vm=vm,
                desired_disk_total=desired_disk_total,
            )

        disk_summary = (
            f"{len(disk_entries)} disks ({disks_created} created, "
            f"{disks_updated} updated, {disks_deleted} deleted)"
        )
        await _send_virtual_disk_payload(
            websocket=websocket,
            websocket_lock=websocket_lock,
            use_websocket=use_websocket,
            payload={
                "object": "virtual_disk",
                "type": "sync",
                "data": {
                    "completed": True,
                    "increment_count": "yes" if disks_created > 0 else "no",
                    "rowid": _virtual_disk_rowid(vm_name),
                    "name": vm_name,
                    "sync_status": completed_html,
                    "disks": disk_summary,
                },
            },
        )

        if disks_created > 0:
            return VmDiskSyncOutcome(
                state="created",
                disks_created=disks_created,
                disks_updated=disks_updated,
                disks_deleted=disks_deleted,
                parent_vm_disk_updated=parent_vm_disk_updated,
            )
        if disks_updated > 0 or disks_deleted > 0 or parent_vm_disk_updated:
            return VmDiskSyncOutcome(
                state="updated",
                disks_created=disks_created,
                disks_updated=disks_updated,
                disks_deleted=disks_deleted,
                parent_vm_disk_updated=parent_vm_disk_updated,
            )
        return VmDiskSyncOutcome(state="skipped")
    except Exception as error:
        logger.error("Error syncing disks for VM %s: %s", vm_name, error)
        await _send_virtual_disk_payload(
            websocket=websocket,
            websocket_lock=websocket_lock,
            use_websocket=use_websocket,
            payload={
                "object": "virtual_disk",
                "type": "sync",
                "data": {
                    "completed": True,
                    "rowid": _virtual_disk_rowid(vm_name),
                    "name": vm_name,
                    "sync_status": failed_html,
                    "disks": str(error),
                },
            },
        )
        return VmDiskSyncOutcome(state="skipped")


async def create_virtual_disks(  # noqa: C901
    netbox_session: object,
    pxs: ProxmoxSessionsDep,
    cluster_status: list[object] | None,
    cluster_resources: list[dict[str, object]] | None = None,
    tag: object | None = None,
    websocket: object | None = None,
    use_websocket: bool = False,
    use_css: bool = False,
    netbox_vm_id: int | None = None,
    netbox_vm_ids: list[int] | None = None,
    fetch_max_concurrency: int | None = None,
) -> dict[str, object]:
    """
    Sync virtual disks for existing Virtual Machines in NetBox.

    Queries NetBox for VMs that have cf_proxmox_vm_id set, fetches their
    disk configuration from Proxmox, and creates/updates Virtual Disk objects.

    When ``netbox_vm_id`` is provided only that single VM is processed.
    When ``netbox_vm_ids`` is provided only those VMs are processed.
    """
    nb = netbox_session
    undefined_html = return_status_html("undefined", use_css)
    syncing_html = return_status_html("syncing", use_css)
    completed_html = return_status_html("completed", use_css)
    failed_html = return_status_html("failed", use_css)

    tag_refs = []
    if tag:
        tag_refs = [
            {
                "name": getattr(tag, "name", None),
                "slug": getattr(tag, "slug", None),
                "color": getattr(tag, "color", None),
            }
        ]
        tag_refs = [t for t in tag_refs if t.get("name") and t.get("slug")]

    target_vm_ids: list[int] | None = None
    if netbox_vm_ids is not None:
        target_vm_ids = netbox_vm_ids
    elif netbox_vm_id is not None:
        target_vm_ids = [netbox_vm_id]

    if target_vm_ids is not None:
        logger.info("Starting virtual disks sync for NetBox VM ids=%s", target_vm_ids)
    else:
        logger.info("Starting virtual disks sync for existing VMs")

    try:
        if target_vm_ids is not None:
            vms = await list_netbox_virtual_machines_by_ids(nb, target_vm_ids)
            vms = require_selected_netbox_vm_coverage(
                vms,
                target_vm_ids,
                operation="virtual-disk sync",
            )
        else:
            vms = await _list_all_vms_with_proxmox_id(nb)
    except Exception as e:
        if target_vm_ids is not None:
            raise
        logger.error(f"Error fetching VMs from NetBox: {e}")
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "virtual_disk",
                    "type": "sync",
                    "data": {
                        "completed": True,
                        "error": f"Error fetching VMs: {e}",
                    },
                }
            )
        return {"count": 0, "created": 0, "updated": 0, "skipped": 0, "error": str(e)}

    vms = [vm for vm in vms if extract_proxmox_vmid(vm)]

    storage_index: dict[tuple[str, str], dict] = {}
    try:
        storage_records = await rest_list_async(nb, "/api/plugins/proxbox/storage/")
        storage_index = build_storage_index(storage_records)
    except Exception as error:
        logger.warning("Error loading storage records for virtual disk sync: %s", error)

    if not vms:
        logger.info("No VMs found with cf_proxmox_vm_id set")
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "virtual_disk",
                    "type": "sync",
                    "data": {
                        "completed": True,
                        "message": "No VMs found with cf_proxmox_vm_id set",
                    },
                }
            )
        return {"count": 0, "created": 0, "updated": 0, "skipped": 0}

    total_vms = len(vms)
    created = 0
    updated = 0
    skipped = 0
    websocket_lock = asyncio.Lock() if use_websocket and websocket else None

    logger.info(f"Found {total_vms} VMs with cf_proxmox_vm_id to process")

    for vm in vms:
        vm_name = str(vm.get("name", "unknown") or "unknown")
        await _send_virtual_disk_payload(
            websocket=websocket,
            websocket_lock=websocket_lock,
            use_websocket=use_websocket,
            payload={
                "object": "virtual_disk",
                "type": "sync",
                "data": {
                    "completed": False,
                    "rowid": _virtual_disk_rowid(vm_name),
                    "name": vm_name,
                    "sync_status": syncing_html,
                    "disks": undefined_html,
                },
            },
        )

    fetch_semaphore = asyncio.Semaphore(_resolve_fetch_concurrency(fetch_max_concurrency))

    async def _fetch_with_limit(vm: dict[str, object]) -> VmDiskFetchResult:
        async with fetch_semaphore:
            return await _fetch_virtual_disk_vm_config(
                vm=vm,
                pxs=pxs,
                cluster_status=cluster_status,
                cluster_resources=cluster_resources,
            )

    fetch_results = await asyncio.gather(*[_fetch_with_limit(vm) for vm in vms])

    ready_to_sync: list[VmDiskFetchResult] = []
    for fetch_result in fetch_results:
        if fetch_result.failure_message:
            skipped += 1
            await _send_virtual_disk_payload(
                websocket=websocket,
                websocket_lock=websocket_lock,
                use_websocket=use_websocket,
                payload={
                    "object": "virtual_disk",
                    "type": "sync",
                    "data": {
                        "completed": True,
                        "rowid": _virtual_disk_rowid(fetch_result.vm_name),
                        "name": fetch_result.vm_name,
                        "sync_status": failed_html,
                        "disks": fetch_result.failure_message,
                    },
                },
            )
            continue
        ready_to_sync.append(fetch_result)

    write_semaphore = asyncio.Semaphore(_resolve_write_concurrency())

    async def _sync_with_limit(fetch_result: VmDiskFetchResult) -> VmDiskSyncOutcome:
        async with write_semaphore:
            return await _sync_virtual_disks_for_vm(
                nb=nb,
                fetched_vm=fetch_result,
                storage_index=storage_index,
                tag_refs=tag_refs,
                websocket=websocket,
                websocket_lock=websocket_lock,
                use_websocket=use_websocket,
                completed_html=completed_html,
                failed_html=failed_html,
            )

    sync_outcomes = await asyncio.gather(*[_sync_with_limit(result) for result in ready_to_sync])
    for outcome in sync_outcomes:
        if outcome.state == "created":
            created += 1
        elif outcome.state == "updated":
            updated += 1
        else:
            skipped += 1

    result = {
        "count": total_vms,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }

    logger.info(f"Virtual disks sync complete: {result}")

    await _send_virtual_disk_payload(
        websocket=websocket,
        websocket_lock=websocket_lock,
        use_websocket=use_websocket,
        payload={"object": "virtual_disk", "end": True},
    )

    return result
