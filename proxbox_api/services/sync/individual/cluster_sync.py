"""Individual Cluster sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async
from proxbox_api.services.netbox_writers import upsert_cluster, upsert_cluster_type
from proxbox_api.services.run_session import SyncContext
from proxbox_api.services.sync.cluster_links import (
    CLUSTER_PATH,
    ENDPOINT_PATH,
    ClusterBindingError,
    NativeClusterResponse,
    resolve_plugin_endpoint_id,
)
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.services.sync.vm_helpers import to_mapping


def _one_exact_read(rows: list[object], expected_id: int) -> dict[str, object]:
    if len(rows) != 1:
        raise ClusterBindingError("Dry-run identity read must return exactly one record.")
    row = to_mapping(rows[0])
    if type(row.get("id")) is not int or row["id"] != expected_id:
        raise ClusterBindingError("Dry-run response differs from its exact requested identity.")
    return row


async def _dry_run_native_cluster(nb: object, source: object) -> dict[str, object] | None:
    endpoint_id = await resolve_plugin_endpoint_id(nb, source)
    endpoints = await rest_list_async(nb, ENDPOINT_PATH, query={"id": endpoint_id, "limit": 2})
    endpoint = _one_exact_read(endpoints, endpoint_id)
    link = endpoint.get("netbox_cluster")
    if link is None:
        return None
    native = NativeClusterResponse.model_validate(
        {"id": link} if type(link) is int else to_mapping(link)
    )
    clusters = await rest_list_async(nb, CLUSTER_PATH, query={"id": native.id, "limit": 2})
    return _one_exact_read(clusters, native.id)


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
    mode = getattr(px, "mode", None) or "cluster"
    proxmox_resource["mode"] = mode

    if dry_run:
        netbox_object = await _dry_run_native_cluster(ctx.nb, px)
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
            mode=mode,
            tag_refs=tag_refs,
        )

        cluster_result = await upsert_cluster(
            ctx.nb,
            cluster_name=cluster_name,
            cluster_type_id=getattr(cluster_type_result.record, "id", None),
            mode=mode,
            tag_refs=tag_refs,
            endpoint_source=px,
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
