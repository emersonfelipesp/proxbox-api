"""Virtual disks synchronization service from Proxmox to NetBox."""

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualDiskSyncState, ProxmoxVmConfigInput
from proxbox_api.routes.proxmox import get_vm_config
from proxbox_api.services.sync.storage_links import (
    build_storage_index,
    find_storage_record,
    storage_name_from_volume_id,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils import return_status_html


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


async def create_virtual_disks(
    netbox_session,
    pxs: ProxmoxSessionsDep,
    cluster_status,
    cluster_resources=None,
    tag=None,
    websocket=None,
    use_websocket=False,
    use_css=False,
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
                    "object": "virtual_disk",
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

    logger.info(f"Found {total_vms} VMs with cf_proxmox_vm_id to process")

    for vm in vms:
        vmid = _extract_proxmox_vmid(vm)
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

            if not node_name and cluster_resources:
                for cluster in cluster_resources:
                    cluster_name_key = (
                        list(cluster.keys())[0] if isinstance(cluster, dict) else None
                    )
                    if cluster_name_key:
                        resources = cluster[cluster_name_key]
                        for resource in resources:
                            if _normalize_vmid(resource.get("vmid")) == vmid:
                                node_name = resource.get("node")
                                cluster_name = cluster_name_key
                                break
                    if node_name:
                        break

            vm_type = "qemu"

            if not node_name:
                logger.warning(f"No node found for VM {vm_name} (vmid: {vmid}), skipping disk sync")
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
                storage_name = disk_entry.storage_name or storage_name_from_volume_id(disk_entry.storage)
                storage_record = find_storage_record(
                    storage_index,
                    cluster_name=cluster_name,
                    storage_name=storage_name,
                )
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
                        "storage": storage_record.get("id") if storage_record else None,
                        "description": disk_entry.description,
                        "tags": tag_refs,
                    },
                    schema=NetBoxVirtualDiskSyncState,
                    current_normalizer=lambda record: {
                        "virtual_machine": record.get("virtual_machine"),
                        "name": record.get("name"),
                        "size": record.get("size"),
                        "storage": record.get("storage"),
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
