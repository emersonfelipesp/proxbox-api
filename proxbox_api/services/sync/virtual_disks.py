"""Virtual disks synchronization service from Proxmox to NetBox."""

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    rest_list_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxVirtualDiskSyncState,
    ProxmoxVmConfigInput,
)
from proxbox_api.routes.proxmox import get_vm_config
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils import return_status_html


async def create_virtual_disks(
    netbox_session,
    pxs: ProxmoxSessionsDep,
    cluster_status,
    tag=None,
    websocket=None,
    use_websocket=False,
    use_css=False,
    sync_process=None,
):
    """
    Sync virtual disks for existing Virtual Machines in NetBox.

    Queries NetBox for VMs that have cf_proxmox_vm_id set, fetches their
    disk configuration from Proxmox, and creates/updates Virtual Disk objects.
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

    logger.info("Starting virtual disks sync for existing VMs")

    vms = await rest_list_async(
        nb,
        "/api/virtualization/virtual-machines/",
        query={"cf_proxmox_vm_id__isnull": False},
    )

    if not vms:
        logger.info("No VMs found with cf_proxmox_vm_id set")
        return {"count": 0, "created": 0, "updated": 0, "skipped": 0}

    total_vms = len(vms)
    created = 0
    updated = 0
    skipped = 0

    logger.info(f"Found {total_vms} VMs with cf_proxmox_vm_id to process")

    for vm in vms:
        vmid = vm.get("cf_proxmox_vm_id")
        vm_name = vm.get("name", "unknown")
        vm_id = vm.get("id")

        if not vmid:
            skipped += 1
            continue

        initial_disk_json = {
            "completed": False,
            "rowid": f"{vm_name}-disks",
            "name": vm_name,
            "sync_status": syncing_html,
            "disks": undefined_html,
        }

        if use_websocket and websocket:
            await websocket.send_json(
                {"object": "virtual_disk", "type": "sync", "data": initial_disk_json}
            )

        try:
            cluster_name = None
            if vm.get("cluster"):
                cluster_name = (
                    vm.get("cluster").get("name") if isinstance(vm.get("cluster"), dict) else None
                )

            if not cluster_name:
                for cs in cluster_status:
                    cs_name = getattr(cs, "name", None)
                    if cs_name:
                        cluster_name = cs_name
                        break

            node_name = (
                vm.get("device", {}).get("name") if isinstance(vm.get("device"), dict) else None
            )
            vm_type = "qemu"

            if not node_name:
                logger.warning(f"No node found for VM {vm_name}, skipping disk sync")
                skipped += 1
                if use_websocket and websocket:
                    await websocket.send_json(
                        {
                            "object": "virtual_disk",
                            "type": "sync",
                            "data": {
                                "completed": True,
                                "rowid": f"{vm_name}-disks",
                                "name": vm_name,
                                "sync_status": failed_html,
                                "disks": "No node associated",
                            },
                        }
                    )
                continue

            vm_config = None
            try:
                vm_config = await get_vm_config(
                    pxs=pxs,
                    cluster_status=cluster_status,
                    node=node_name,
                    type=vm_type,
                    vmid=vmid,
                )
            except Exception as e:
                logger.error(f"Error getting VM config for {vm_name}: {e}")

            if not vm_config:
                logger.warning(f"Could not get VM config for VM {vm_name} (vmid: {vmid})")
                skipped += 1
                if use_websocket and websocket:
                    await websocket.send_json(
                        {
                            "object": "virtual_disk",
                            "type": "sync",
                            "data": {
                                "completed": True,
                                "rowid": f"{vm_name}-disks",
                                "name": vm_name,
                                "sync_status": failed_html,
                                "disks": "Config not available",
                            },
                        }
                    )
                continue

            vm_config_obj = ProxmoxVmConfigInput.model_validate(vm_config)
            disk_entries = vm_config_obj.disks

            disks_created = 0
            disks_updated = 0

            for disk_entry in disk_entries:
                result = await rest_reconcile_async(
                    nb,
                    "/api/virtualization/virtual-disks/",
                    lookup={
                        "virtual_machine_id": vm_id,
                        "name": disk_entry.name,
                    },
                    payload={
                        "virtual_machine": vm_id,
                        "name": disk_entry.name,
                        "size": disk_entry.size,
                        "description": disk_entry.description,
                        "tags": tag_refs,
                    },
                    schema=NetBoxVirtualDiskSyncState,
                    current_normalizer=lambda record: {
                        "virtual_machine": record.get("virtual_machine"),
                        "name": record.get("name"),
                        "size": record.get("size"),
                        "description": record.get("description"),
                        "tags": record.get("tags"),
                    },
                )

                if result.get("created", False):
                    disks_created += 1
                else:
                    disks_updated += 1

            if disks_created > 0:
                created += 1
            elif disks_updated > 0:
                updated += 1
            else:
                skipped += 1

            disk_summary = (
                f"{len(disk_entries)} disks ({disks_created} created, {disks_updated} updated)"
            )

            if use_websocket and websocket:
                await websocket.send_json(
                    {
                        "object": "virtual_disk",
                        "type": "sync",
                        "data": {
                            "completed": True,
                            "increment_count": "yes" if disks_created > 0 else "no",
                            "rowid": f"{vm_name}-disks",
                            "name": vm_name,
                            "sync_status": completed_html,
                            "disks": disk_summary,
                        },
                    }
                )

        except Exception as e:
            logger.error(f"Error syncing disks for VM {vm_name}: {e}")
            skipped += 1
            if use_websocket and websocket:
                await websocket.send_json(
                    {
                        "object": "virtual_disk",
                        "type": "sync",
                        "data": {
                            "completed": True,
                            "rowid": f"{vm_name}-disks",
                            "name": vm_name,
                            "sync_status": failed_html,
                            "disks": str(e),
                        },
                    }
                )

    result = {
        "count": total_vms,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }

    logger.info(f"Virtual disks sync complete: {result}")

    if use_websocket and websocket:
        await websocket.send_json({"object": "virtual_disk", "end": True})

    return result
