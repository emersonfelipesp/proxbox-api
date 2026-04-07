"""Virtual machine snapshots synchronization service from Proxmox to NetBox."""

from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    RestRecord,
    rest_create_async,
    rest_list_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxSnapshotSyncState,
)
from proxbox_api.services.proxmox_helpers import get_vm_snapshots
from proxbox_api.services.sync.storage_links import (
    build_storage_index,
    find_storage_record,
    storage_name_from_volume_id,
)
from proxbox_api.services.sync.vmid_helpers import (
    extract_proxmox_vm_type,
    extract_proxmox_vmid,
    normalize_vmid,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils import return_status_html

_DEFAULT_FETCH_CONCURRENCY = max(1, int(os.getenv("PROXBOX_PROXMOX_FETCH_CONCURRENCY", "8")))
_DEFAULT_VM_SYNC_CONCURRENCY = max(1, int(os.getenv("PROXBOX_NETBOX_WRITE_CONCURRENCY", "4")))


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


async def create_netbox_snapshots(
    snapshot: object,
    netbox_session: object,
    vmid: str | int,
    node: str,
    storage_record: dict | None = None,
) -> object | None:
    """
    Create or update a snapshot in NetBox.

    Args:
        snapshot: Snapshot data from Proxmox
        netbox_session: NetBox session
        vmid: Proxmox VM ID
        node: Proxmox node name

    Returns:
        NetBox snapshot object or None on failure
    """
    nb = netbox_session

    try:
        if not isinstance(snapshot, dict):
            return None

        snapshot_name = snapshot.get("name")
        if not snapshot_name:
            return None

        vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": int(vmid)},
        )
        virtual_machine = vms[0] if vms else None

        if not virtual_machine:
            return None

        snaptime = None
        st = snapshot.get("snaptime")
        if st:
            try:
                snaptime = datetime.fromtimestamp(st).isoformat()
            except (ValueError, OSError, TypeError) as error:
                logger.debug("Invalid snapshot snaptime for vmid=%s: %s (%s)", vmid, st, error)

        subtype = snapshot.get("type", "qemu")

        snapshot_payload = {
            "virtual_machine": virtual_machine.get("id"),
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

        netbox_snapshot = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/snapshots/",
            lookup={"vmid": int(vmid), "name": snapshot_name, "node": node},
            payload=snapshot_payload,
            schema=NetBoxSnapshotSyncState,
            current_normalizer=lambda record: {
                "virtual_machine": record.get("virtual_machine"),
                "proxmox_storage": record.get("proxmox_storage"),
                "name": record.get("name"),
                "description": record.get("description"),
                "vmid": record.get("vmid"),
                "node": record.get("node"),
                "snaptime": record.get("snaptime"),
                "parent": record.get("parent"),
                "subtype": record.get("subtype"),
                "status": record.get("status"),
            },
        )

        if netbox_snapshot and hasattr(netbox_snapshot, "id"):
            await rest_create_async(
                nb,
                "/api/extras/journal-entries/",
                {
                    "assigned_object_type": "netbox_proxbox.vmsnapshot",
                    "assigned_object_id": netbox_snapshot.id,
                    "kind": "info",
                    "comments": f"Snapshot '{snapshot_name}' synced for VM {vmid} on node {node}",
                },
            )

        return netbox_snapshot

    except Exception as error:
        error_detail = getattr(error, "detail", str(error))
        error_msg = f"{type(error).__name__}: {error_detail}"
        logger.error(f"Error creating NetBox snapshot for VM {vmid}: {error_msg}")
        return None


async def process_snapshots_batch(snapshot_tasks: list, batch_size: int = 10) -> tuple[list, int]:
    """
    Process a list of snapshot tasks in batches.

    Returns:
        (successful_reconcile_results, failure_count)
    """
    results: list = []
    failures = 0
    for i in range(0, len(snapshot_tasks), batch_size):
        batch = snapshot_tasks[i : i + batch_size]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)
        for r in batch_results:
            if isinstance(r, Exception):
                failures += 1
            elif r is not None:
                results.append(r)
    return results, failures


def _resolve_snapshot_node_from_resources(
    vmid: int,
    cluster_resources: list[dict[str, object]] | None,
) -> tuple[str | None, str | None]:
    """Resolve node and cluster from cluster resource listings."""
    if not cluster_resources:
        return None, None

    for cluster in cluster_resources:
        if not isinstance(cluster, dict):
            continue
        cluster_items = list(cluster.items())
        if not cluster_items:
            continue
        cluster_key, resources = cluster_items[0]
        if not isinstance(resources, list):
            continue
        for resource in resources:
            if normalize_vmid(resource.get("vmid")) == vmid:
                return resource.get("node"), cluster_key
    return None, None


def _resolve_snapshot_node_from_status(
    node: str | None,
    cluster_status: list | None,
) -> tuple[str | None, str | None]:
    """Fallback node resolution from cluster status."""
    if not cluster_status:
        return node, None

    node_name = node
    cluster_name = None
    if not node_name:
        for cs in cluster_status:
            if hasattr(cs, "node_list") and cs.node_list:
                return cs.node_list[0].name, getattr(cs, "name", None)

    if node_name:
        for cs in cluster_status:
            if getattr(cs, "node_list", None):
                if any(node_item.name == node_name for node_item in cs.node_list):
                    cluster_name = getattr(cs, "name", None)
                    break
    return node_name, cluster_name


def _resolve_snapshot_node_context(
    vmid: int,
    node: str | None,
    cluster_status: list | None,
    cluster_resources: list[dict[str, object]] | None,
) -> tuple[str | None, str | None]:
    """Resolve the node and cluster name for a VM snapshot sync."""
    node_name, cluster_name = _resolve_snapshot_node_from_resources(
        vmid,
        cluster_resources,
    )
    if node_name:
        return node_name, cluster_name
    return _resolve_snapshot_node_from_status(node, cluster_status)


async def _collect_snapshot_tasks_for_vm(
    pxs: list,
    *,
    node_name: str,
    proxmox_type: str,
    vmid: int,
    fetch_semaphore: asyncio.Semaphore,
    nb,
    storage_record: dict | None,
) -> tuple[list, set[str]]:
    """Fetch snapshots from all Proxmox sessions and build NetBox reconcile tasks."""
    snapshot_tasks = []
    proxmox_snapshot_names: set[str] = set()

    async def _fetch_snapshots_for_endpoint(proxmox):
        async with fetch_semaphore:
            result = get_vm_snapshots(
                session=proxmox.session,
                node=node_name,
                vm_type=proxmox_type,
                vmid=int(vmid),
            )
            if inspect.isawaitable(result):
                return await result
            return result

    snapshot_results = await asyncio.gather(
        *[_fetch_snapshots_for_endpoint(proxmox) for proxmox in pxs],
        return_exceptions=True,
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
            if snap_name:
                proxmox_snapshot_names.add(snap_name)
                snapshot_tasks.append(
                    create_netbox_snapshots(
                        snapshot,
                        nb,
                        vmid,
                        node_name,
                        storage_record=storage_record,
                    )
                )

    return snapshot_tasks, proxmox_snapshot_names


async def _sync_single_vm_snapshots(
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
) -> tuple[int, int, int]:
    """Sync snapshots for a single VM. Returns (created, updated, skipped)."""
    created = 0
    updated = 0
    skipped = 0
    proxmox_snapshot_names: set[str] = set()

    vmid = extract_proxmox_vmid(vm)
    vm_name = vm.get("name", "unknown")

    if not vmid:
        return (0, 0, 1, set())

    if use_websocket and websocket:
        await websocket.send_json(
            {
                "object": "snapshot",
                "type": "sync",
                "data": {
                    "sync_status": undefined_html,
                    "name": vm_name,
                    "netbox_id": vm.get("id"),
                    "status": "Processing...",
                },
            }
        )

    try:
        proxmox_type = extract_proxmox_vm_type(vm) or vm.get("type", "qemu")
        if proxmox_type not in ("qemu", "lxc"):
            proxmox_type = "qemu"

        node_name, cluster_name = _resolve_snapshot_node_context(
            vmid,
            node,
            cluster_status,
            cluster_resources,
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
            return (0, 0, 1, set())

        storage_record = await _resolve_snapshot_storage_record(
            nb,
            vm_id=int(vmid),
            cluster_name=cluster_name,
            storage_index=storage_index,
        )

        snapshot_tasks, proxmox_snapshot_names = await _collect_snapshot_tasks_for_vm(
            pxs,
            node_name=node_name,
            proxmox_type=proxmox_type,
            vmid=vmid,
            fetch_semaphore=fetch_semaphore,
            nb=nb,
            storage_record=storage_record,
        )

        if snapshot_tasks:
            results, failures = await process_snapshots_batch(snapshot_tasks)
            created = len(results)
            skipped = failures
        else:
            skipped = 1

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "snapshot",
                    "type": "sync",
                    "data": {
                        "completed": True,
                        "name": vm_name,
                        "netbox_id": vm.get("id"),
                        "snapshots_found": len(proxmox_snapshot_names),
                        "sync_status": completed_html,
                    },
                }
            )

    except Exception as e:
        logger.error(f"Error syncing snapshots for VM {vm_name} ({vmid}): {e}")
        skipped = 1
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

    return (created, updated, skipped, proxmox_snapshot_names)


async def _list_all_vms_with_proxmox_id(
    nb,
    batch_size: int = 500,
) -> list[RestRecord]:
    """List all VMs from NetBox with pagination handling."""
    all_vms = []
    offset = 0

    while True:
        vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"limit": batch_size, "offset": offset},
        )
        if not vms:
            break
        all_vms.extend(vms)

        if len(vms) < batch_size:
            break
        offset += batch_size

    return all_vms


async def create_virtual_machine_snapshots(  # noqa: C901
    netbox_session: object,
    pxs: ProxmoxSessionsDep,
    cluster_status: list[object] | None,
    cluster_resources: list[dict[str, object]] | None = None,
    tag: object | None = None,
    vmid: int | None = None,
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
        vms = await _list_all_vms_with_proxmox_id(nb)
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
        return {"count": 0, "created": 0, "updated": 0, "skipped": 0, "error": str(e)}

    vms = [vm for vm in vms if extract_proxmox_vmid(vm)]

    if not vms:
        logger.warning("No VMs found with cf_proxmox_vm_id set")
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
    fetch_semaphore = asyncio.Semaphore(fetch_max_concurrency or _DEFAULT_FETCH_CONCURRENCY)
    vm_sync_semaphore = asyncio.Semaphore(_DEFAULT_VM_SYNC_CONCURRENCY)

    async def _sync_vm_with_semaphore(vm):
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
            )

    sync_tasks = [_sync_vm_with_semaphore(vm) for vm in vms]
    results = await asyncio.gather(*sync_tasks, return_exceptions=True)

    proxmox_snapshot_names_by_vmid: dict[int, set[str]] = {}

    for vm, result in zip(vms, results):
        if isinstance(result, Exception):
            logger.warning("Snapshot sync task failed: %s", result)
            skipped += 1
        else:
            c, u, s, snapshot_names = result
            created += c
            updated += u
            skipped += s
            vm_vmid = extract_proxmox_vmid(vm)
            if vm_vmid:
                try:
                    proxmox_snapshot_names_by_vmid[int(vm_vmid)] = snapshot_names
                except (ValueError, TypeError):
                    pass

    if delete_nonexistent_snapshot and proxmox_snapshot_names_by_vmid:
        try:
            netbox_snapshots = await rest_list_async(nb, "/api/plugins/proxbox/snapshots/")
            for nb_snapshot in netbox_snapshots or []:
                snapshot_vmid = nb_snapshot.vmid
                snapshot_name = nb_snapshot.name
                if snapshot_vmid and snapshot_name:
                    proxmox_names = proxmox_snapshot_names_by_vmid.get(snapshot_vmid, set())
                    if snapshot_name not in proxmox_names:
                        try:
                            nb_snapshot.delete()
                            deleted += 1
                            logger.info(
                                "Deleted orphaned snapshot %s for VM vmid=%s",
                                snapshot_name,
                                snapshot_vmid,
                            )
                        except Exception as del_err:
                            logger.warning(
                                "Failed to delete orphaned snapshot %s for VM vmid=%s: %s",
                                snapshot_name,
                                snapshot_vmid,
                                del_err,
                            )
        except Exception as list_err:
            logger.warning("Error loading NetBox snapshots for cleanup: %s", list_err)

    logger.info(
        f"Snapshot sync completed: {created} created/updated, {skipped} skipped, {deleted} deleted"
    )

    return {
        "count": total_vms,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "deleted": deleted,
    }
