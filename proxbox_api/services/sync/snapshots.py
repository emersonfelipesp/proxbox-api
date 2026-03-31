"""Virtual machine snapshots synchronization service from Proxmox to NetBox."""

import asyncio
import os
from datetime import datetime

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
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
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils import return_status_html

_DEFAULT_FETCH_CONCURRENCY = max(1, int(os.getenv("PROXBOX_FETCH_MAX_CONCURRENCY", "8")))


def _normalize_vmid(vmid):
    """Normalize VMID values for safe cross-system comparisons."""
    if vmid is None:
        return None
    vmid_str = str(vmid).strip()
    return vmid_str or None


def _extract_proxmox_vmid(vm: dict) -> str | None:
    """Extract Proxmox VMID from NetBox VM payload across known field layouts."""
    top_level_keys = (
        "cf_proxmox_vm_id",
        "proxmox_vm_id",
        "cf_proxmox_vmid",
        "proxmox_vmid",
    )
    for key in top_level_keys:
        normalized = _normalize_vmid(vm.get(key))
        if normalized:
            return normalized

    custom_fields = vm.get("custom_fields")
    if isinstance(custom_fields, dict):
        custom_field_keys = (
            "proxmox_vm_id",
            "cf_proxmox_vm_id",
            "proxmox_vmid",
            "cf_proxmox_vmid",
        )
        for key in custom_field_keys:
            normalized = _normalize_vmid(custom_fields.get(key))
            if normalized:
                return normalized
    return None


async def _load_storage_index(netbox_session) -> dict[tuple[str, str], dict]:
    nb = netbox_session
    try:
        storage_records = await rest_list_async(nb, "/api/plugins/proxbox/storage/")
    except Exception as error:
        logger.warning("Error loading storage records for snapshot sync: %s", error)
        return {}
    return build_storage_index(storage_records)


async def _resolve_snapshot_storage_record(
    netbox_session,
    *,
    vm_id: int,
    cluster_name: str | None,
    storage_index: dict[tuple[str, str], dict],
) -> dict | None:
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
    snapshot,
    netbox_session,
    vmid,
    node,
    storage_record: dict | None = None,
):
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
            except (ValueError, OSError):
                pass

        subtype = snapshot.get("type", "qemu")

        snapshot_payload = {
            "virtual_machine": virtual_machine.get("id"),
            "storage": storage_record.get("id") if storage_record else None,
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
                "storage": record.get("storage"),
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
        logger.error(f"Error creating NetBox snapshot for VM {vmid}: {error}")
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


async def create_virtual_machine_snapshots(
    netbox_session,
    pxs: ProxmoxSessionsDep,
    cluster_status,
    cluster_resources=None,
    tag=None,
    vmid: int | None = None,
    node: str | None = None,
    websocket=None,
    use_websocket=False,
    use_css=False,
    fetch_max_concurrency: int | None = None,
):
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
        vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
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
        return {"count": 0, "created": 0, "updated": 0, "skipped": 0, "error": str(e)}

    vms_with_proxmox_id = [vm for vm in vms if _extract_proxmox_vmid(vm)]
    vms = vms_with_proxmox_id

    if not vms:
        logger.info("No VMs found with cf_proxmox_vm_id set")
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

    total_vms = len(vms)
    created = 0
    updated = 0
    skipped = 0
    storage_index = await _load_storage_index(nb)

    logger.info(f"Found {total_vms} VMs with cf_proxmox_vm_id to process")
    fetch_semaphore = asyncio.Semaphore(fetch_max_concurrency or _DEFAULT_FETCH_CONCURRENCY)

    for vm in vms:
        vmid = _extract_proxmox_vmid(vm)
        vm_name = vm.get("name", "unknown")

        if not vmid:
            skipped += 1
            continue

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
            proxmox_type = vm.get("type", "qemu")
            if proxmox_type not in ("qemu", "lxc"):
                proxmox_type = "qemu"

            node_name = node
            cluster_name = None

            if not node_name and cluster_resources:
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
                        if _normalize_vmid(resource.get("vmid")) == vmid:
                            node_name = resource.get("node")
                            cluster_name = cluster_key
                            break
                    if node_name:
                        break

            if not node_name:
                for cs in cluster_status:
                    if hasattr(cs, "node_list") and cs.node_list:
                        node_name = cs.node_list[0].name
                        cluster_name = getattr(cs, "name", None)
                        break

            if not node_name:
                logger.warning(
                    f"No node found for VM {vm_name} (vmid: {vmid}), skipping snapshot sync"
                )
                skipped += 1
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
                continue

            if not cluster_name:
                for cs in cluster_status:
                    if getattr(cs, "node_list", None):
                        if any(node_item.name == node_name for node_item in cs.node_list):
                            cluster_name = getattr(cs, "name", None)
                            break

            storage_record = await _resolve_snapshot_storage_record(
                nb,
                vm_id=int(vmid),
                cluster_name=cluster_name,
                storage_index=storage_index,
            )

            snapshot_tasks = []
            proxmox_snapshot_names = set()

            async def _fetch_snapshots_for_endpoint(proxmox):
                async with fetch_semaphore:
                    return await asyncio.to_thread(
                        lambda: get_vm_snapshots(
                            session=proxmox.session,
                            node=node_name,
                            vm_type=proxmox_type,
                            vmid=int(vmid),
                        )
                    )

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

            if snapshot_tasks:
                results, failures = await process_snapshots_batch(snapshot_tasks)
                created += len(results)
                if failures:
                    skipped += failures
            else:
                skipped += 1

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
            skipped += 1
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

    logger.info(f"Snapshot sync completed: {created} created/updated, {skipped} skipped")

    return {
        "count": total_vms,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }
