"""Individual Cluster sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async
from proxbox_api.services.netbox_writers import upsert_cluster, upsert_cluster_type


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
        IndividualSyncResponse dict whose ``action`` reports the actual
        drift-detection outcome (``created`` / ``updated`` / ``unchanged``)
        for the cluster itself.
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
        cluster_type_result = await upsert_cluster_type(
            nb,
            mode="cluster",
            tag_refs=tag_refs,
        )
        cluster_result = await upsert_cluster(
            nb,
            cluster_name=cluster_name,
            cluster_type_id=getattr(cluster_type_result.record, "id", None),
            mode="cluster",
            tag_refs=tag_refs,
        )

        netbox_object = (
            cluster_result.record.serialize()
            if hasattr(cluster_result.record, "serialize")
            else None
        )

        return {
            "object_type": "cluster",
            "action": cluster_result.status,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": [
                {"object_type": "cluster_type", "action": cluster_type_result.status},
            ],
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
