"""Synchronize Proxmox storage definitions into NetBox plugin storage objects."""

from __future__ import annotations

import asyncio

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxStorageSyncState
from proxbox_api.services.proxmox_helpers import (
    dump_models,
    get_storage_config,
    get_storage_list,
)


async def create_storages(  # noqa: C901
    *,
    netbox_session: object,
    pxs: list[object] | None,
    tag: object,
    websocket: object | None = None,
    use_websocket: bool = False,
    fetch_concurrency: int = 8,
) -> list[dict[str, object]]:
    """Create or update plugin storage rows for each Proxmox endpoint storage.

    The Proxmox /storage endpoint may include enough information to reconcile a
    storage record directly. When the list payload is incomplete, we fall back to
    fetching /storage/{storage_id} for the full configuration.
    """
    nb = netbox_session
    tag_refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    tag_refs = [tag_ref for tag_ref in tag_refs if tag_ref.get("name") and tag_ref.get("slug")]
    synced: list[dict[str, object]] = []

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
        logger.warning("Unable to prefetch NetBox clusters for storage sync: %s", error)

    # Build mapping of cluster_name -> proxmox session
    proxmoxs_map: dict[str, object] = {}
    for px in pxs or []:
        name = str(getattr(px, "name", "") or "").strip()
        if name:
            proxmoxs_map[name] = px

    async def _fetch_cluster_storage_items(
        proxmox: object,
    ) -> tuple[str, list[dict[str, object]]]:
        cluster_name = str(getattr(proxmox, "name", "") or "").strip() or "unknown"
        async with fetch_sem:
            storage_items = await asyncio.to_thread(lambda: dump_models(get_storage_list(proxmox)))
        return cluster_name, storage_items

    cluster_storage_items = await asyncio.gather(
        *[_fetch_cluster_storage_items(proxmox) for proxmox in pxs],
        return_exceptions=True,
    )

    unique_payloads: dict[tuple[str, str], dict[str, object]] = {}

    def _needs_storage_config(config: dict[str, object]) -> bool:
        return any(
            key not in config for key in ("type", "content", "path", "nodes", "shared", "disable")
        )

    for cluster_data in cluster_storage_items:
        if isinstance(cluster_data, Exception):
            logger.warning("Error fetching storage list from Proxmox endpoint: %s", cluster_data)
            continue
        cluster_name, storage_items = cluster_data
        proxmox = proxmoxs_map.get(cluster_name)
        for storage_item in storage_items:
            storage_name = str(storage_item.get("storage", "") or "").strip()
            if not storage_name:
                continue
            config = dict(storage_item)
            if proxmox and _needs_storage_config(config):
                try:
                    async with fetch_sem:
                        fetched_config = await asyncio.to_thread(
                            get_storage_config,
                            proxmox,
                            storage_name,
                        )
                except Exception as error:
                    logger.warning(
                        "Error fetching storage config for %s/%s: %s",
                        cluster_name,
                        storage_name,
                        error,
                    )
                else:
                    if fetched_config:
                        config.update(fetched_config)
            unique_payloads[(cluster_name, storage_name)] = config

    for (cluster_name, storage_name), config in unique_payloads.items():
        cluster_id = cluster_id_map.get(cluster_name)
        if cluster_id is None:
            logger.debug("Skipping storages for unknown NetBox cluster '%s'", cluster_name)
            continue

        payload = {
            "cluster": cluster_id,
            "name": storage_name,
            "storage_type": config.get("type"),
            "content": config.get("content"),
            "path": config.get("path"),
            "nodes": config.get("nodes"),
            "shared": bool(config.get("shared")),
            "enabled": not bool(config.get("disable")),
            "tags": tag_refs,
        }
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
