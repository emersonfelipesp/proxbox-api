"""Individual Cluster sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async
from proxbox_api.services.netbox_writers import upsert_cluster, upsert_cluster_type
from proxbox_api.services.run_session import SyncContext
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session


async def sync_cluster_individual(
    ctx: SyncContext,
    cluster_name: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Sync a single Cluster from Proxmox to NetBox.

    Args:
        ctx: Per-run :class:`SyncContext` carrying the NetBox session,
            the Proxmox sessions list, the Proxbox tag, the plugin
            settings, and the per-run identifier.
        cluster_name: Name of the cluster to sync. Used to resolve the
            matching Proxmox session from ``ctx.px_sessions``.
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict whose ``action`` reports the actual
        drift-detection outcome (``created`` / ``updated`` / ``unchanged``)
        for the cluster itself.
    """
    from proxbox_api.services.sync.individual.base import BaseIndividualSyncService

    now = datetime.now(timezone.utc)
    proxmox_resource: dict[str, object] = {
        "name": cluster_name,
        "mode": "cluster",
        "proxmox_last_updated": now.isoformat(),
    }

    px = resolve_proxmox_session(ctx.px_sessions, cluster_name)
    if px is None:
        return {
            "object_type": "cluster",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": dry_run,
            "dependencies_synced": [],
            "error": f"No Proxmox session found for cluster: {cluster_name}",
        }

    service = BaseIndividualSyncService(ctx.nb, px, ctx.tag)
    tag_refs = service.tag_refs

    if dry_run:
        existing = await rest_list_async(
            ctx.nb,
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
            ctx.nb,
            mode="cluster",
            tag_refs=tag_refs,
        )
        cluster_result = await upsert_cluster(
            ctx.nb,
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
