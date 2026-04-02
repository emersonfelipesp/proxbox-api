"""Individual Cluster sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxClusterSyncState, NetBoxClusterTypeSyncState


async def sync_cluster_individual(
    nb: object,
    px: object,
    tag: object,
    cluster_name: str,
    dry_run: bool = False,
) -> dict:
    """Sync a single Cluster from Proxmox to NetBox.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        cluster_name: Name of the cluster to sync.
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict.
    """
    from proxbox_api.services.sync.individual.base import BaseIndividualSyncService

    service = BaseIndividualSyncService(nb, px, tag)
    tag_refs = service.tag_refs
    now = datetime.now(timezone.utc)

    proxmox_resource: dict[str, object] = {
        "name": cluster_name,
        "mode": "cluster",
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        existing = await rest_list_async(
            nb,
            "/api/virtualization/clusters/",
            query={"name": cluster_name},
        )
        netbox_object = None
        if existing:
            netbox_object = existing[0].serialize() if hasattr(existing[0], "serialize") else None
        return {
            "object_type": "cluster",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": [],
            "error": None,
        }

    try:
        cluster_type = await rest_reconcile_async(
            nb,
            "/api/virtualization/cluster-types/",
            lookup={"slug": "cluster"},
            payload={
                "name": "Cluster",
                "slug": "cluster",
                "description": "Proxmox cluster mode",
                "tags": tag_refs,
                "custom_fields": {"proxmox_last_updated": now.isoformat()},
            },
            schema=NetBoxClusterTypeSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "description": record.get("description"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
            },
        )

        cluster = await rest_reconcile_async(
            nb,
            "/api/virtualization/clusters/",
            lookup={"name": cluster_name},
            payload={
                "name": cluster_name,
                "type": getattr(cluster_type, "id", None),
                "description": "Proxmox cluster.",
                "tags": tag_refs,
                "custom_fields": {"proxmox_last_updated": now.isoformat()},
            },
            schema=NetBoxClusterSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "type": record.get("type"),
                "description": record.get("description"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
            },
        )

        netbox_object = cluster.serialize() if hasattr(cluster, "serialize") else None
        action = "created" if getattr(cluster, "id", None) else "updated"

        return {
            "object_type": "cluster",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": [],
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "cluster",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }
