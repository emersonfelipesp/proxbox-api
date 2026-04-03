"""Individual Replication sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxReplicationSyncState
from proxbox_api.services.proxmox_helpers import get_cluster_replication
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService


async def sync_replication_individual(
    nb: object,
    px: object,
    tag: object,
    replication_id: str,
    auto_create_vm: bool = True,
    dry_run: bool = False,
) -> dict:
    """Sync a single Replication from Proxmox to NetBox.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        replication_id: Replication job ID (e.g., '100-1').
        auto_create_vm: Whether to auto-create the VM if it doesn't exist.
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict.
    """
    service = BaseIndividualSyncService(nb, px, tag)
    tag_refs = service.tag_refs
    now = datetime.now(timezone.utc)

    try:
        replications = get_cluster_replication(px)
    except Exception:
        replications = []

    target_replication = None
    for rep in replications:
        if str(rep.get("id", "")) == replication_id:
            target_replication = rep
            break

    if not target_replication:
        return {
            "object_type": "replication",
            "action": "error",
            "proxmox_resource": {"replication_id": replication_id},
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": f"Replication job {replication_id} not found in Proxmox",
        }

    guest_vmid = target_replication.get("guest")
    if not guest_vmid:
        return {
            "object_type": "replication",
            "action": "error",
            "proxmox_resource": target_replication,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": f"Guest VM ID not found in replication job {replication_id}",
        }

    proxmox_resource: dict[str, object] = {
        "vmid": guest_vmid,
        "replication_data": target_replication,
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        existing_vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": guest_vmid},
        )
        netbox_object = None
        if existing_vms:
            vm_id = getattr(existing_vms[0], "id", None)
            if vm_id:
                existing = await rest_list_async(
                    nb,
                    "/api/plugins/proxbox/replications/",
                    query={"replication_id": replication_id},
                )
                if existing:
                    netbox_object = (
                        existing[0].serialize() if hasattr(existing[0], "serialize") else None
                    )

        vm_dep: dict[str, object] = {"object_type": "vm", "vmid": guest_vmid}
        return {
            "object_type": "replication",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": [vm_dep],
            "error": None,
        }

    try:
        existing_vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": guest_vmid},
        )
        if not existing_vms:
            if auto_create_vm:
                from proxbox_api.services.sync.individual.vm_sync import sync_vm_individual

                cluster_name = getattr(px, "name", "unknown")
                await sync_vm_individual(
                    nb, px, tag, cluster_name, None, "qemu", guest_vmid, dry_run=False
                )
                existing_vms = await rest_list_async(
                    nb,
                    "/api/virtualization/virtual-machines/",
                    query={"cf_proxmox_vm_id": guest_vmid},
                )
            else:
                return {
                    "object_type": "replication",
                    "action": "error",
                    "proxmox_resource": proxmox_resource,
                    "netbox_object": None,
                    "dry_run": False,
                    "dependencies_synced": [],
                    "error": f"VM with vmid={guest_vmid} not found in NetBox",
                }

        vm_record = existing_vms[0]
        vm_id = getattr(vm_record, "id", None)
        if vm_id is None:
            return {
                "object_type": "replication",
                "action": "error",
                "proxmox_resource": proxmox_resource,
                "netbox_object": None,
                "dry_run": False,
                "dependencies_synced": [],
                "error": f"Could not resolve VM ID for vmid={guest_vmid}",
            }

        node_name = target_replication.get("target")
        node_id = None
        if node_name:
            existing_nodes = await rest_list_async(
                nb,
                "/api/plugins/proxbox/nodes/",
                query={"name": node_name},
            )
            if existing_nodes:
                node_id = getattr(existing_nodes[0], "id", None)

        replication_payload: dict[str, object] = {
            "virtual_machine": vm_id,
            "proxmox_node": node_id,
            "replication_id": target_replication.get("id"),
            "guest": target_replication.get("guest"),
            "target": target_replication.get("target"),
            "job_type": target_replication.get("type"),
            "schedule": target_replication.get("schedule"),
            "rate": target_replication.get("rate"),
            "comment": target_replication.get("comment"),
            "disable": target_replication.get("disable"),
            "source": target_replication.get("source"),
            "jobnum": target_replication.get("jobnum"),
            "remove_job": target_replication.get("remove_job"),
            "tags": tag_refs,
        }

        existing_replications = await rest_list_async(
            nb,
            "/api/plugins/proxbox/replications/",
            query={"replication_id": replication_id},
        )
        replication_record = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/replications/",
            lookup={"replication_id": replication_id},
            payload=replication_payload,
            schema=NetBoxReplicationSyncState,
            current_normalizer=lambda record: {
                "virtual_machine": record.get("virtual_machine"),
                "proxmox_node": record.get("proxmox_node"),
                "replication_id": record.get("replication_id"),
                "guest": record.get("guest"),
                "target": record.get("target"),
                "job_type": record.get("job_type"),
                "schedule": record.get("schedule"),
                "rate": record.get("rate"),
                "comment": record.get("comment"),
                "disable": record.get("disable"),
                "source": record.get("source"),
                "jobnum": record.get("jobnum"),
                "remove_job": record.get("remove_job"),
                "tags": record.get("tags"),
            },
        )

        netbox_object = (
            replication_record.serialize()
            if hasattr(replication_record, "serialize")
            else None
        )
        action = "updated" if existing_replications else "created"

        return {
            "object_type": "replication",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": [
                {"object_type": "vm", "vmid": guest_vmid, "action": action},
            ],
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "replication",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }
