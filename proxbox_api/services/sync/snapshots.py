"""Virtual machine snapshots synchronization service from Proxmox to NetBox."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    RestRecord,
    rest_bulk_delete_async,
    rest_bulk_reconcile_async,
    rest_list_async,
    rest_list_paginated_async,
)
from proxbox_api.proxmox_to_netbox.models import NetBoxSnapshotSyncState
from proxbox_api.runtime_settings import get_int
from proxbox_api.services.proxmox_helpers import get_vm_snapshots
from proxbox_api.services.sync._helpers import _extract_fk_id
from proxbox_api.services.sync.storage_links import (
    build_storage_index,
    find_storage_record,
    storage_name_from_volume_id,
)
from proxbox_api.services.sync.vm_helpers import (
    list_netbox_virtual_machines_by_ids,
    relation_id,
    to_mapping,
)
from proxbox_api.services.sync.vmid_helpers import (
    extract_proxmox_endpoint_id,
    extract_proxmox_node,
    extract_proxmox_vm_type,
    extract_proxmox_vmid,
    normalize_vmid,
    select_proxmox_sessions_by_endpoint,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils import return_status_html


def _resolve_fetch_concurrency() -> int:
    return get_int(
        settings_key="proxmox_fetch_concurrency",
        env="PROXBOX_PROXMOX_FETCH_CONCURRENCY",
        default=8,
        minimum=1,
    )


def _resolve_vm_sync_concurrency() -> int:
    return get_int(
        settings_key="netbox_write_concurrency",
        env="PROXBOX_NETBOX_WRITE_CONCURRENCY",
        default=4,
        minimum=1,
    )


async def _load_storage_index(netbox_session: object) -> dict[tuple[str, str], dict[str, object]]:
    nb = netbox_session
    try:
        storage_records = await rest_list_async(nb, "/api/plugins/proxbox/storage/")
    except Exception as error:
        logger.warning("Error loading storage records for snapshot sync: %s", error)
        return {}
    return build_storage_index(storage_records)


async def _resolve_snapshot_storage_record(
    netbox_session: object,
    *,
    vm_id: int,
    cluster_name: str | None,
    storage_index: dict[tuple[str, str], dict[str, object]],
) -> dict[str, object] | None:
    nb = netbox_session
    try:
        virtual_disks = await rest_list_async(
            nb,
            "/api/virtualization/virtual-disks/",
            query={"virtual_machine_id": int(vm_id), "ordering": "name"},
        )
    except Exception as error:
        logger.warning(
            "Error loading virtual disks for snapshot storage resolution (vmid=%s): %s",
            vm_id,
            error,
        )
        return None

    for disk in virtual_disks or []:
        disk_name = disk.get("name")
        storage_name = storage_name_from_volume_id(disk_name)
        if not storage_name:
            continue
        storage_record = find_storage_record(
            storage_index,
            cluster_name=cluster_name,
            storage_name=storage_name,
        )
        if storage_record:
            return storage_record

    return None


async def build_snapshot_payload(
    snapshot: object,
    vmid: str | int,
    node: str,
    netbox_vm_id: int,
    storage_record: dict | None = None,
    proxmox_type: str = "qemu",
) -> dict | None:
    """
    Build a snapshot payload dict for bulk operations (no NetBox writes).

    Args:
        snapshot: Snapshot data from Proxmox
        vmid: Proxmox VM ID
        node: Proxmox node name
        netbox_vm_id: NetBox VM ID (pre-fetched)
        storage_record: Storage record dict or None

    Returns:
        Snapshot payload dict or None if invalid
    """
    try:
        if not isinstance(snapshot, dict):
            return None

        snapshot_name = snapshot.get("name")
        if not snapshot_name:
            return None

        snaptime = None
        st = snapshot.get("snaptime")
        if st:
            try:
                snaptime = datetime.fromtimestamp(st).isoformat()
            except (ValueError, OSError, TypeError) as error:
                logger.debug("Invalid snapshot snaptime for vmid=%s: %s (%s)", vmid, st, error)

        subtype = proxmox_type if proxmox_type in ("qemu", "lxc") else "qemu"

        return {
            "virtual_machine": netbox_vm_id,
            "proxmox_storage": storage_record.get("id") if storage_record else None,
            "name": snapshot_name,
            "description": snapshot.get("description", ""),
            "vmid": int(vmid),
            "node": node,
            "snaptime": snaptime,
            "parent": snapshot.get("parent"),
            "subtype": subtype,
            "status": "active",
        }

    except Exception as error:
        logger.debug("Error building snapshot payload for VM %s: %s", vmid, error)
        return None


def _resolve_snapshot_node_from_resources(
    vmid: int,
    cluster_resources: list[dict[str, object]] | None,
    cluster_name: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve node and cluster from cluster resource listings."""
    if not cluster_resources:
        return None, None

    vmid_key = normalize_vmid(vmid)
    candidates = _snapshot_node_candidates_for_vmid(cluster_resources, vmid_key)
    if not candidates:
        return None, None
    if cluster_name:
        normalized_cluster = cluster_name.strip().casefold()
        matching = [
            (node_name, candidate_cluster_name)
            for node_name, candidate_cluster_name in candidates
            if str(candidate_cluster_name or "").strip().casefold() == normalized_cluster
        ]
        return matching[0] if len(matching) == 1 else (None, cluster_name)
    if len(candidates) == 1:
        return candidates[0]
    return None, None


def _snapshot_node_candidates_for_vmid(
    cluster_resources: list[dict[str, object]],
    vmid_key: str,
) -> list[tuple[str | None, str | None]]:
    candidates: list[tuple[str | None, str | None]] = []
    for cluster in cluster_resources:
        if not isinstance(cluster, dict):
            continue
        cluster_items = list(cluster.items())
        if not cluster_items:
            continue
        cluster_key, resources = cluster_items[0]
        if not isinstance(resources, list):
            continue
        candidates.extend(_snapshot_node_candidates_in_cluster(resources, cluster_key, vmid_key))
    return candidates


def _snapshot_node_candidates_in_cluster(
    resources: list[object],
    cluster_key: object,
    vmid_key: str,
) -> list[tuple[str | None, str | None]]:
    candidates: list[tuple[str | None, str | None]] = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if normalize_vmid(resource.get("vmid")) == vmid_key:
            candidates.append((resource.get("node"), cluster_key))
    return candidates


def _resolve_snapshot_node_from_status(
    node: str | None,
    cluster_status: list | None,
    *,
    cluster_name: str | None = None,
    require_unique_match: bool = False,
) -> tuple[str | None, str | None]:
    """Fallback node resolution without crossing a VM's cluster boundary."""
    if not cluster_status:
        return node, cluster_name if node else None

    statuses = list(cluster_status)
    if cluster_name:
        normalized_cluster = cluster_name.strip().casefold()
        statuses = [
            status
            for status in statuses
            if str(getattr(status, "name", "") or "").strip().casefold() == normalized_cluster
        ]
        if not statuses:
            return (None, cluster_name) if require_unique_match else (node, cluster_name)

    if node:
        matching_statuses = [
            status
            for status in statuses
            if any(
                getattr(node_item, "name", None) == node
                for node_item in (getattr(status, "node_list", None) or [])
            )
        ]
        if len(matching_statuses) == 1:
            return node, getattr(matching_statuses[0], "name", None) or cluster_name
        if require_unique_match:
            return None, cluster_name
        return node, cluster_name

    candidates = [
        (getattr(node_item, "name", None), getattr(status, "name", None))
        for status in statuses
        for node_item in (getattr(status, "node_list", None) or [])
        if getattr(node_item, "name", None)
    ]
    unique_candidates = list(dict.fromkeys(candidates))
    if len(unique_candidates) == 1:
        return unique_candidates[0]
    if not require_unique_match and unique_candidates:
        return unique_candidates[0]
    return None, cluster_name


def _resolve_snapshot_node_context(
    vmid: int,
    node: str | None,
    cluster_name: str | None,
    cluster_status: list | None,
    cluster_resources: list[dict[str, object]] | None,
    *,
    require_unique_match: bool = False,
) -> tuple[str | None, str | None]:
    """Resolve the node and cluster name for a VM snapshot sync."""
    node_name, resource_cluster_name = _resolve_snapshot_node_from_resources(
        vmid,
        cluster_resources,
        cluster_name=cluster_name,
    )
    resolved_cluster_name = resource_cluster_name or cluster_name
    if node_name:
        return node_name, resolved_cluster_name
    return _resolve_snapshot_node_from_status(
        node,
        cluster_status,
        cluster_name=resolved_cluster_name,
        require_unique_match=require_unique_match,
    )


async def _collect_snapshot_payloads_for_vm(
    pxs: list,
    *,
    node_name: str,
    proxmox_type: str,
    vmid: int,
    netbox_vm_id: int,
    fetch_semaphore: asyncio.Semaphore,
    storage_record: dict | None,
) -> tuple[list[dict], set[str], bool]:
    """Return payloads, names, and whether every intended Proxmox read succeeded."""
    snapshot_payloads: list[dict] = []
    proxmox_snapshot_names: set[str] = set()

    async def _fetch_snapshots_for_endpoint(proxmox: object) -> list[dict[str, object]]:
        async with fetch_semaphore:
            result = get_vm_snapshots(
                session=proxmox,
                node=node_name,
                vm_type=proxmox_type,
                vmid=int(vmid),
                raise_on_error=True,
            )
            if inspect.isawaitable(result):
                return await result
            return result

    snapshot_results = await asyncio.gather(
        *[_fetch_snapshots_for_endpoint(proxmox) for proxmox in pxs],
        return_exceptions=True,
    )
    collection_complete = bool(snapshot_results) and all(
        not isinstance(result, Exception) for result in snapshot_results
    )
    logger.debug(
        "VM vmid=%s node=%s type=%s: fetched snapshots from %d Proxmox endpoint(s), %d result set(s)",
        vmid,
        node_name,
        proxmox_type,
        len(pxs),
        len(snapshot_results),
    )
    for result in snapshot_results:
        if isinstance(result, Exception):
            logger.warning(
                "Error getting snapshots for VM %s on node %s: %s",
                vmid,
                node_name,
                result,
            )
            continue
        for snapshot in result:
            snap_name = snapshot.get("name")
            if not snap_name:
                continue
            if snap_name == "current":
                logger.debug(
                    "Skipping 'current' pseudo-snapshot for vmid=%s on node=%s",
                    vmid,
                    node_name,
                )
                continue
            proxmox_snapshot_names.add(snap_name)
            payload = await build_snapshot_payload(
                snapshot,
                vmid,
                node_name,
                netbox_vm_id,
                storage_record=storage_record,
                proxmox_type=proxmox_type,
            )
            if payload is not None:
                snapshot_payloads.append(payload)

    return snapshot_payloads, proxmox_snapshot_names, collection_complete


async def _sync_single_vm_snapshots(  # noqa: C901
    vm: object,
    nb,
    pxs: list,
    cluster_status: list | None,
    cluster_resources: list[dict[str, object]] | None,
    node: str | None,
    storage_index: dict,
    fetch_semaphore: asyncio.Semaphore,
    tag_refs: list[dict],
    use_websocket: bool,
    websocket: object | None,
    undefined_html: str,
    completed_html: str,
    failed_html: str,
    explicitly_selected: bool,
) -> tuple[list[dict], set[str], bool]:
    """Return payloads, names, and a proven-complete discovery coverage flag."""
    snapshot_payloads: list[dict] = []
    proxmox_snapshot_names: set[str] = set()

    vm_data = to_mapping(vm)
    vmid = extract_proxmox_vmid(vm_data)
    vm_name = vm_data.get("name", "unknown")
    netbox_vm_id = vm_data.get("id")
    endpoint_id = extract_proxmox_endpoint_id(vm_data)
    stored_node = node or extract_proxmox_node(vm_data)
    vm_cluster_name = None
    cluster = vm_data.get("cluster")
    if isinstance(cluster, dict):
        vm_cluster_name = str(cluster.get("name") or "").strip() or None

    if not vmid:
        return ([], set(), False)

    if use_websocket and websocket:
        await websocket.send_json(
            {
                "object": "snapshot",
                "type": "sync",
                "data": {
                    "sync_status": undefined_html,
                    "name": vm_name,
                    "netbox_id": netbox_vm_id,
                    "status": "Processing...",
                },
            }
        )

    try:
        proxmox_type = _normalize_snapshot_vm_type(extract_proxmox_vm_type(vm) or vm.get("type"))

        node_name, cluster_name = _resolve_snapshot_node_context(
            vmid,
            stored_node,
            vm_cluster_name,
            cluster_status,
            cluster_resources,
            require_unique_match=explicitly_selected,
        )

        logger.debug(
            "VM %s (vmid=%s): resolved node=%s cluster=%s type=%s",
            vm_name,
            vmid,
            node_name,
            cluster_name,
            proxmox_type,
        )

        if not node_name:
            logger.warning(
                "Snapshot sync skipped for VM %s (vmid=%s): no node found in cluster_resources or cluster_status",
                vm_name,
                vmid,
            )
            if use_websocket and websocket:
                await websocket.send_json(
                    {
                        "object": "snapshot",
                        "type": "sync",
                        "data": {
                            "completed": True,
                            "name": vm_name,
                            "sync_status": failed_html,
                            "error": "No node associated",
                        },
                    }
                )
            return ([], set(), False)

        storage_record = await _resolve_snapshot_storage_record(
            nb,
            vm_id=int(netbox_vm_id),
            cluster_name=cluster_name,
            storage_index=storage_index,
        )

        effective_pxs = _snapshot_sessions_for_vm(
            pxs,
            endpoint_id=endpoint_id,
            cluster_name=cluster_name,
            node_name=node_name,
            require_unique_match=explicitly_selected,
        )
        if not effective_pxs:
            logger.warning(
                "Snapshot sync skipped for VM %s (vmid=%s endpoint_id=%s): "
                "no unambiguous endpoint session is available",
                vm_name,
                vmid,
                endpoint_id,
            )
            return ([], set(), False)

        (
            snapshot_payloads,
            proxmox_snapshot_names,
            collection_complete,
        ) = await _collect_snapshot_payloads_for_vm(
            effective_pxs,
            node_name=node_name,
            proxmox_type=proxmox_type,
            vmid=vmid,
            netbox_vm_id=netbox_vm_id,
            fetch_semaphore=fetch_semaphore,
            storage_record=storage_record,
        )

        if not collection_complete:
            logger.warning(
                "Snapshot discovery was incomplete for VM %s (vmid=%s); stale cleanup is disabled",
                vm_name,
                vmid,
            )
            return snapshot_payloads, proxmox_snapshot_names, False

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "snapshot",
                    "type": "sync",
                    "data": {
                        "completed": True,
                        "name": vm_name,
                        "netbox_id": netbox_vm_id,
                        "snapshots_found": len(proxmox_snapshot_names),
                        "sync_status": completed_html,
                    },
                }
            )

        return snapshot_payloads, proxmox_snapshot_names, True

    except Exception as e:
        logger.error(f"Error syncing snapshots for VM {vm_name} ({vmid}): {e}")
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "snapshot",
                    "type": "sync",
                    "data": {
                        "completed": True,
                        "error": str(e),
                        "name": vm_name,
                        "sync_status": failed_html,
                    },
                }
            )

        return (snapshot_payloads, proxmox_snapshot_names, False)


def _normalize_snapshot_vm_type(proxmox_type: object) -> str:
    if proxmox_type in ("qemu", "lxc"):
        return str(proxmox_type)
    return "qemu"


def _snapshot_sessions_for_vm(
    pxs: list,
    *,
    endpoint_id: int | None,
    cluster_name: str | None,
    node_name: str | None,
    require_unique_match: bool = False,
) -> list:
    """Select the Proxmox session that can own this VM's snapshot calls."""
    effective_pxs = select_proxmox_sessions_by_endpoint(pxs, endpoint_id)
    if endpoint_id is not None:
        return effective_pxs
    matched_pxs = [
        px for px in pxs if px.name and (px.name == cluster_name or px.name == node_name)
    ]
    if require_unique_match:
        return matched_pxs if len(matched_pxs) == 1 else []
    return matched_pxs if matched_pxs else list(pxs)


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


async def create_virtual_machine_snapshots(  # noqa: C901
    netbox_session: object,
    pxs: ProxmoxSessionsDep,
    cluster_status: list[object] | None,
    cluster_resources: list[dict[str, object]] | None = None,
    tag: object | None = None,
    vmid: int | list[int] | None = None,
    netbox_vm_ids: list[int] | None = None,
    node: str | None = None,
    websocket: object | None = None,
    use_websocket: bool = False,
    use_css: bool = False,
    fetch_max_concurrency: int | None = None,
    delete_nonexistent_snapshot: bool = False,
) -> dict[str, object]:
    """
    Sync snapshots for existing Virtual Machines in NetBox.

    Queries NetBox for VMs that have cf_proxmox_vm_id set, fetches their
    snapshots from Proxmox, and creates/updates VMSnapshot objects.
    """
    nb = netbox_session
    undefined_html = return_status_html("undefined", use_css)
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

    logger.info("Starting virtual machine snapshots sync")

    try:
        vms = (
            await _list_all_vms_with_proxmox_id(nb)
            if netbox_vm_ids is None
            else await list_netbox_virtual_machines_by_ids(nb, netbox_vm_ids)
        )
    except Exception as e:
        logger.error(f"Error fetching VMs from NetBox: {e}")
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "snapshot",
                    "type": "sync",
                    "data": {
                        "completed": True,
                        "error": f"Error fetching VMs: {e}",
                    },
                }
            )
        if netbox_vm_ids is not None:
            if isinstance(e, ProxboxException):
                raise
            raise ProxboxException(
                message="Unable to resolve explicitly selected NetBox VMs",
                detail=str(e),
                http_status_code=502,
            ) from e
        return {"count": 0, "created": 0, "updated": 0, "skipped": 0, "error": str(e)}

    logger.info("Fetched %d VMs from NetBox before proxmox_vm_id filtering", len(vms))

    if netbox_vm_ids is not None:
        selected_netbox_ids = set(netbox_vm_ids)
        vms = [vm for vm in vms if relation_id(to_mapping(vm).get("id")) in selected_netbox_ids]
        resolved_netbox_ids = {
            vm_id for vm in vms if (vm_id := relation_id(to_mapping(vm).get("id"))) is not None
        }
        missing_netbox_ids = selected_netbox_ids - resolved_netbox_ids
        if missing_netbox_ids:
            raise ProxboxException(
                message="Unable to resolve explicitly selected NetBox VMs",
                detail=f"NetBox did not return selected VM id(s): {sorted(missing_netbox_ids)}.",
                http_status_code=502,
            )
    elif vmid is not None:
        selected_vmids = {
            normalized
            for value in (vmid if isinstance(vmid, list) else [vmid])
            if (normalized := normalize_vmid(value)) is not None
        }
        vms = [vm for vm in vms if extract_proxmox_vmid(vm) in selected_vmids]
    else:
        vms = [vm for vm in vms if extract_proxmox_vmid(vm)]

    logger.info("After proxmox_vm_id filtering: %d VMs remain for snapshot sync", len(vms))

    if not vms:
        logger.warning(
            "No VMs found with proxmox_vm_id custom field set; "
            "ensure the VM sync stage runs before snapshot sync and that the "
            "'proxmox_vm_id' custom field is defined and populated in NetBox"
        )
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "snapshot",
                    "type": "sync",
                    "data": {
                        "completed": True,
                        "message": "No VMs found with cf_proxmox_vm_id set",
                    },
                }
            )
        return {"count": 0, "created": 0, "updated": 0, "skipped": 0}

    logger.info("cluster_resources provided: %s", cluster_resources is not None)
    if cluster_resources:
        logger.debug("cluster_resources content: %s", cluster_resources)

    total_vms = len(vms)
    created = 0
    updated = 0
    skipped = 0
    deleted = 0
    storage_index = await _load_storage_index(nb)

    logger.info(f"Found {total_vms} VMs with cf_proxmox_vm_id to process")

    # Emit structured discovery event so the live panel shows total VM count
    if use_websocket and websocket and hasattr(websocket, "emit_discovery"):
        await websocket.emit_discovery(
            phase="vm-snapshots",
            items=[{"name": str(vm.get("name", "")), "type": "virtual-machine"} for vm in vms],
            message=f"Discovered {total_vms} VM(s) to scan for snapshots",
        )

    fetch_semaphore = asyncio.Semaphore(fetch_max_concurrency or _resolve_fetch_concurrency())
    vm_sync_semaphore = asyncio.Semaphore(_resolve_vm_sync_concurrency())

    async def _sync_vm_with_semaphore(
        vm: object,
    ) -> tuple[list[dict[str, object]], set[str], bool]:
        async with vm_sync_semaphore:
            return await _sync_single_vm_snapshots(
                vm=vm,
                nb=nb,
                pxs=pxs,
                cluster_status=cluster_status,
                cluster_resources=cluster_resources,
                node=node,
                storage_index=storage_index,
                fetch_semaphore=fetch_semaphore,
                tag_refs=tag_refs,
                use_websocket=use_websocket,
                websocket=websocket,
                undefined_html=undefined_html,
                completed_html=completed_html,
                failed_html=failed_html,
                explicitly_selected=netbox_vm_ids is not None,
            )

    sync_tasks = [_sync_vm_with_semaphore(vm) for vm in vms]
    results = await asyncio.gather(*sync_tasks, return_exceptions=True)

    # Collect all snapshot payloads from all VMs, emit per-VM item_progress
    all_snapshot_payloads: list[dict] = []
    proxmox_snapshot_names_by_vm_id: dict[int, set[str]] = {}
    vm_failed = 0

    for idx, (vm, result) in enumerate(zip(vms, results), start=1):
        vm_data = to_mapping(vm)
        vm_name = str(vm_data.get("name", ""))
        if isinstance(result, Exception):
            logger.warning("Snapshot sync task failed: %s", result)
            skipped += 1
            vm_failed += 1
            if use_websocket and websocket and hasattr(websocket, "emit_item_progress"):
                await websocket.emit_item_progress(
                    phase="vm-snapshots",
                    item={"name": vm_name, "type": "virtual-machine"},
                    operation="failed",
                    status="failed",
                    message=f"Snapshot fetch failed for VM '{vm_name}': {result}",
                    progress_current=idx,
                    progress_total=total_vms,
                    error=str(result),
                )
        else:
            snapshot_payloads, snapshot_names, discovery_complete = result
            all_snapshot_payloads.extend(snapshot_payloads)
            netbox_vm_id = vm_data.get("id")
            if discovery_complete:
                try:
                    if netbox_vm_id is not None:
                        proxmox_snapshot_names_by_vm_id[int(netbox_vm_id)] = snapshot_names
                except (ValueError, TypeError):
                    pass
            else:
                skipped += 1
                vm_failed += 1
            snap_count = len(snapshot_payloads)
            if use_websocket and websocket and hasattr(websocket, "emit_item_progress"):
                await websocket.emit_item_progress(
                    phase="vm-snapshots",
                    item={"name": vm_name, "type": "virtual-machine"},
                    operation="updated",
                    status="completed",
                    message=f"Scanned VM '{vm_name}': {snap_count} snapshot(s) found",
                    progress_current=idx,
                    progress_total=total_vms,
                )

    logger.info(
        "Collected %d total snapshot payload(s) from %d VM(s) (%d failed/skipped)",
        len(all_snapshot_payloads),
        total_vms,
        vm_failed,
    )

    # Perform bulk reconciliation if we have payloads
    if all_snapshot_payloads:
        try:
            reconcile_result = await rest_bulk_reconcile_async(
                nb,
                "/api/plugins/proxbox/snapshots/",
                payloads=all_snapshot_payloads,
                # VMID/node/name are endpoint-local. Including the owning NetBox
                # VM prevents a selected endpoint from patching another owner's row.
                lookup_fields=["virtual_machine", "vmid", "name", "node"],
                schema=NetBoxSnapshotSyncState,
                current_normalizer=lambda record: {
                    "vmid": record.get("vmid"),
                    "name": record.get("name"),
                    "node": record.get("node"),
                    "virtual_machine": _extract_fk_id(record.get("virtual_machine")),
                },
            )
            created = reconcile_result.created
            updated = reconcile_result.updated
            logger.info(
                "Snapshot sync completed via bulk operation: created=%s, updated=%s",
                created,
                updated,
            )
        except Exception as e:
            logger.error("Error during bulk snapshot reconciliation: %s", e)
            skipped = len(all_snapshot_payloads)
            vm_failed += 1

    if delete_nonexistent_snapshot and proxmox_snapshot_names_by_vm_id:
        try:
            netbox_snapshots = await rest_list_async(nb, "/api/plugins/proxbox/snapshots/")
            orphan_ids: list[int] = []
            for nb_snapshot in netbox_snapshots or []:
                snapshot_data = to_mapping(nb_snapshot)
                snapshot_name = snapshot_data.get("name")
                snapshot_vm_id = _extract_fk_id(snapshot_data.get("virtual_machine"))
                snapshot_id = (
                    getattr(nb_snapshot, "id", None)
                    if not isinstance(nb_snapshot, dict)
                    else nb_snapshot.get("id")
                )
                if (
                    snapshot_vm_id
                    and snapshot_name
                    and snapshot_id
                    and snapshot_vm_id in proxmox_snapshot_names_by_vm_id
                ):
                    proxmox_names = proxmox_snapshot_names_by_vm_id[snapshot_vm_id]
                    if snapshot_name not in proxmox_names:
                        orphan_ids.append(int(snapshot_id))
                        logger.info(
                            "Marking orphaned snapshot %s (id=%s) for NetBox VM id=%s for bulk deletion",
                            snapshot_name,
                            snapshot_id,
                            snapshot_vm_id,
                        )
            if orphan_ids:
                try:
                    deleted = await rest_bulk_delete_async(
                        nb, "/api/plugins/proxbox/snapshots/", orphan_ids
                    )
                    logger.info("Bulk deleted %d orphaned snapshot(s)", deleted)
                except Exception as del_err:
                    logger.warning("Bulk delete of orphaned snapshots failed: %s", del_err)
        except Exception as list_err:
            logger.warning("Error loading NetBox snapshots for cleanup: %s", list_err)

    logger.info(
        f"Snapshot sync completed: {created} created/updated, {skipped} skipped, {deleted} deleted"
    )

    if use_websocket and websocket and hasattr(websocket, "emit_phase_summary"):
        await websocket.emit_phase_summary(
            phase="vm-snapshots",
            created=created,
            updated=updated,
            deleted=deleted,
            failed=vm_failed,
            skipped=skipped,
            message=(
                f"Snapshot sync completed: {created} created, {updated} updated, "
                f"{deleted} deleted, {skipped} skipped"
            ),
        )

    return {
        "count": total_vms,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "deleted": deleted,
    }
