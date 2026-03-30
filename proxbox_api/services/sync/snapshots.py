"""Virtual machine snapshots synchronization service from Proxmox to NetBox."""

import asyncio
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
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils import return_status_html


async def create_netbox_snapshots(snapshot, netbox_session, vmid, node):
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
    tag=None,
    vmid: int | None = None,
    node: str | None = None,
    websocket=None,
    use_websocket=False,
    use_css=False,
    sync_process=None,
):
    """
    Sync snapshots for existing Virtual Machines in NetBox.

    Queries NetBox for VMs with cf_proxmox_vm_id set, fetches their
    snapshots from Proxmox, and creates/updates VMSnapshot objects.
    """
    nb = netbox_session
    undefined_html = return_status_html("undefined", use_css)

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
        if vmid:
            vms = await rest_list_async(
                nb,
                "/api/virtualization/virtual-machines/",
                query={"cf_proxmox_vm_id": int(vmid)},
            )
        else:
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

    vms_with_proxmox_id = [vm for vm in vms if vm.get("cf_proxmox_vm_id")]
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

    logger.info(f"Found {total_vms} VMs with cf_proxmox_vm_id to process")

    for vm in vms:
        vm_id = vm.get("cf_proxmox_vm_id")
        vm_name = vm.get("name", "unknown")

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

            cluster_resources = getattr(cluster_status, "__iter__", lambda: [])()
            target_nodes = [node] if node else []
            if not target_nodes:
                target_nodes = [
                    node_info.get("node")
                    for node_info in cluster_resources
                    if isinstance(node_info, dict)
                ]
            if not target_nodes:
                target_nodes = [vm.get("node")]

            snapshot_tasks = []
            proxmox_snapshot_names = set()

            for target_node in target_nodes:
                if not target_node:
                    continue

                for proxmox in pxs:
                    try:
                        snapshots = get_vm_snapshots(
                            session=proxmox.session,
                            node=target_node,
                            vm_type=proxmox_type,
                            vmid=int(vm_id),
                        )

                        for snapshot in snapshots:
                            snap_name = snapshot.get("name")
                            if snap_name:
                                proxmox_snapshot_names.add(snap_name)
                                snapshot_tasks.append(
                                    create_netbox_snapshots(snapshot, nb, vm_id, target_node)
                                )

                    except Exception as e:
                        logger.warning(
                            f"Error getting snapshots for VM {vm_id} on node {target_node}: {e}"
                        )
                        continue

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
                        },
                    }
                )

        except Exception as e:
            logger.error(f"Error syncing snapshots for VM {vm_name} ({vm_id}): {e}")
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
