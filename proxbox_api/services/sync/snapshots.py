"""Virtual machine snapshots synchronization service from Proxmox to NetBox."""

from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    RestRecord,
    rest_bulk_reconcile_async,
    rest_list_async,
)
from proxbox_api.proxmox_to_netbox.models import NetBoxSnapshotSyncState
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


def _extract_fk_id(value: object) -> object:
    """Return the integer ID from a nested FK dict, or the value itself."""
    if isinstance(value, dict):
        return value.get("id")
    return value


def _extract_choice_value(value: object) -> object:
    """Return the raw choice string from a nested choice dict, or the value itself."""
    if isinstance(value, dict):
        return value.get("value")
    return value


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

        subtype = snapshot.get("type", "qemu")

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


async def _collect_snapshot_payloads_for_vm(
    pxs: list,
    *,
    node_name: str,
    proxmox_type: str,
    vmid: int,
    netbox_vm_id: int,
    fetch_semaphore: asyncio.Semaphore,
    storage_record: dict | None,
) -> tuple[list[dict], set[str]]:
    """Fetch snapshots from all Proxmox sessions and build payload dicts for bulk reconcile."""
    snapshot_payloads: list[dict] = []
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
                payload = await build_snapshot_payload(
                    snapshot,
                    vmid,
                    node_name,
                    netbox_vm_id,
                    storage_record=storage_record,
                )
                if payload is not None:
                    snapshot_payloads.append(payload)

    return snapshot_payloads, proxmox_snapshot_names


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
) -> tuple[list[dict], set[str]]:
    """Sync snapshots for a single VM. Returns (payload_list, proxmox_snapshot_names)."""
    snapshot_payloads: list[dict] = []
    proxmox_snapshot_names: set[str] = set()

    vmid = extract_proxmox_vmid(vm)
    vm_name = vm.get("name", "unknown")
    netbox_vm_id = vm.get("id")

    if not vmid:
        return ([], set())

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
            return ([], set())

        storage_record = await _resolve_snapshot_storage_record(
            nb,
            vm_id=int(netbox_vm_id),
            cluster_name=cluster_name,
            storage_index=storage_index,
        )

        snapshot_payloads, proxmox_snapshot_names = await _collect_snapshot_payloads_for_vm(
            pxs,
            node_name=node_name,
            proxmox_type=proxmox_type,
            vmid=vmid,
            netbox_vm_id=netbox_vm_id,
            fetch_semaphore=fetch_semaphore,
            storage_record=storage_record,
        )

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

    return (snapshot_payloads, proxmox_snapshot_names)


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

    logger.info("Fetched %d VMs from NetBox before proxmox_vm_id filtering", len(vms))

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
            items=[
                {"name": str(vm.get("name", "")), "type": "virtual-machine"}
                for vm in vms
            ],
            message=f"Discovered {total_vms} VM(s) to scan for snapshots",
        )

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

    # Collect all snapshot payloads from all VMs, emit per-VM item_progress
    all_snapshot_payloads: list[dict] = []
    proxmox_snapshot_names_by_vmid: dict[int, set[str]] = {}
    vm_failed = 0

    for idx, (vm, result) in enumerate(zip(vms, results), start=1):
        vm_name = str(vm.get("name", ""))
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
            snapshot_payloads, snapshot_names = result
            all_snapshot_payloads.extend(snapshot_payloads)
            vm_vmid = extract_proxmox_vmid(vm)
            if vm_vmid:
                try:
                    proxmox_snapshot_names_by_vmid[int(vm_vmid)] = snapshot_names
                except (ValueError, TypeError):
                    pass
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

    # Perform bulk reconciliation if we have payloads
    if all_snapshot_payloads:
        try:
            reconcile_result = await rest_bulk_reconcile_async(
                nb,
                "/api/plugins/proxbox/snapshots/",
                payloads=all_snapshot_payloads,
                lookup_fields=["vmid", "name", "node"],
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
                            await nb_snapshot.delete()
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
