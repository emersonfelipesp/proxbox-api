"""Synchronize Proxmox storage definitions into NetBox plugin storage objects."""

from __future__ import annotations

import asyncio
from typing import Any

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxStorageSyncState
from proxbox_api.services.proxmox_helpers import dump_models, get_storage_list


async def create_storages(
    *,
    netbox_session: Any,
    pxs: list[Any] | None,
    tag: Any,
    websocket: Any | None = None,
    use_websocket: bool = False,
    fetch_concurrency: int = 8,
) -> list[dict[str, Any]]:
    """Create or update plugin storage rows for each Proxmox endpoint storage."""
    nb = netbox_session
    tag_refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    tag_refs = [tag_ref for tag_ref in tag_refs if tag_ref.get("name") and tag_ref.get("slug")]
    synced: list[dict[str, Any]] = []

    if not pxs:
        logger.info("No Proxmox sessions available for storage sync")
        return synced

    fetch_sem = asyncio.Semaphore(max(1, int(fetch_concurrency)))

    # Pre-fetch all clusters from NetBox to build a name -> id mapping
    cluster_id_map: dict[str, int] = {}
    try:
        clusters = await rest_list_async(nb, "/api/virtualization/clusters/")
        for cluster in clusters:
            cluster_name = str(cluster.get("name", "")).strip()
            cluster_id = cluster.get("id")
            if cluster_name and cluster_id:
                cluster_id_map[cluster_name] = cluster_id
    except Exception as error:
        # If we can't fetch clusters, we'll try to continue and let individual
        # storage syncs fail gracefully
        logger.warning("Unable to prefetch NetBox clusters for storage sync: %s", error)

    async def _fetch_cluster_storages(proxmox: Any) -> tuple[str, list[dict[str, Any]]]:
        cluster_name = str(getattr(proxmox, "name", "") or "").strip() or "unknown"
        async with fetch_sem:
            storages = await asyncio.to_thread(lambda: dump_models(get_storage_list(proxmox)))
        return cluster_name, storages

    cluster_storage_sets = await asyncio.gather(
        *[_fetch_cluster_storages(proxmox) for proxmox in pxs],
        return_exceptions=True,
    )

    # Some deployments expose the same cluster via multiple endpoints and/or repeated
    # storage rows in the same API response. Reconcile each (cluster, storage) once per run
    # to avoid duplicate create attempts against the unique DB constraint.
    unique_payloads: dict[tuple[str, str], dict] = {}

    for cluster_data in cluster_storage_sets:
        if isinstance(cluster_data, Exception):
            logger.warning("Error fetching storage list from Proxmox endpoint: %s", cluster_data)
            continue
        cluster_name, storages = cluster_data

        # Skip if we don't have a cluster ID for this cluster name
        cluster_id = cluster_id_map.get(cluster_name)
        if cluster_id is None:
            logger.debug("Skipping storages for unknown NetBox cluster '%s'", cluster_name)
            continue

        for storage in storages:
            storage_name = str(storage.get("storage") or "").strip()
            if not storage_name:
                continue

            payload = {
                "cluster": cluster_id,
                "name": storage_name,
                "storage_type": storage.get("type"),
                "content": storage.get("content"),
                "path": storage.get("path"),
                "nodes": storage.get("nodes"),
                "shared": bool(storage.get("shared")),
                "enabled": not bool(storage.get("disable")),
                "tags": tag_refs,
            }
            unique_payloads.setdefault((cluster_name, storage_name), payload)

    for (cluster_name, storage_name), payload in unique_payloads.items():
        cluster_id = payload["cluster"]
        record = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/storage/",
            lookup={"cluster": cluster_id, "name": storage_name},
            payload=payload,
            schema=NetBoxStorageSyncState,
            current_normalizer=lambda item: {
                "cluster": item.get("cluster", {}).get("id")
                if isinstance(item.get("cluster"), dict)
                else item.get("cluster"),
                "name": item.get("name"),
                "storage_type": item.get("storage_type"),
                "content": item.get("content"),
                "path": item.get("path"),
                "nodes": item.get("nodes"),
                "shared": item.get("shared"),
                "enabled": item.get("enabled"),
                "backups": item.get("backups"),
                "tags": item.get("tags"),
            },
        )
        data = record.serialize()
        synced.append(data)
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "storage",
                    "status": "synced",
                    "message": f"Synced storage {cluster_name}/{storage_name}",
                    "result": {"id": data.get("id"), "name": storage_name},
                }
            )

    return synced
