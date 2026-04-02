"""Individual Node/Device sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.services.proxmox_helpers import get_node_status_individual
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService


async def sync_node_individual(
    nb: object,
    px: object,
    tag: object,
    cluster_name: str,
    node_name: str,
    dry_run: bool = False,
) -> dict:
    """Sync a single Node (device) from Proxmox to NetBox.

    Auto-creates cluster if it doesn't exist.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        cluster_name: Name of the cluster.
        node_name: Name of the node (host) to sync.
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict.
    """
    service = BaseIndividualSyncService(nb, px, tag)
    now = datetime.now(timezone.utc)

    try:
        proxmox_node = get_node_status_individual(px, node_name)
    except Exception:
        proxmox_node = {"name": node_name, "node_type": "unknown"}

    proxmox_resource: dict[str, object] = {
        "name": node_name,
        "cluster": cluster_name,
        "node_data": proxmox_node,
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        from proxbox_api.netbox_rest import rest_list_async

        existing = await rest_list_async(
            nb,
            "/api/dcim/devices/",
            query={"name": node_name},
        )
        netbox_object = None
        if existing:
            netbox_object = existing[0].serialize() if hasattr(existing[0], "serialize") else None

        cluster_dep: dict[str, object] = {"object_type": "cluster", "name": cluster_name}
        return {
            "object_type": "node",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": [cluster_dep],
            "error": None,
        }

    try:
        cluster = await service._get_or_create_cluster(cluster_name)
        manufacturer = await service._get_or_create_manufacturer()
        device_type = await service._get_or_create_device_type(getattr(manufacturer, "id", None))
        node_role = await service._get_or_create_device_role_node()
        site = await service._get_or_create_site(cluster_name)

        device = await service._get_or_create_device(
            device_name=node_name,
            cluster_id=getattr(cluster, "id", None),
            device_type_id=getattr(device_type, "id", None),
            role_id=getattr(node_role, "id", None),
            site_id=getattr(site, "id", None),
        )

        netbox_object = device.serialize() if hasattr(device, "serialize") else None
        action = "created" if getattr(device, "id", None) else "updated"

        dependencies: list[dict] = [
            {"object_type": "cluster", "name": cluster_name, "action": action},
        ]

        return {
            "object_type": "node",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": dependencies,
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "node",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }
