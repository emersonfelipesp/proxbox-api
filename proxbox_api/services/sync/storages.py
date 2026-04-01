"""Synchronize Proxmox storage definitions into NetBox plugin storage objects."""

from __future__ import annotations

import asyncio


from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxStorageSyncState
from proxbox_api.services.proxmox_helpers import dump_models, get_storage_config, get_storage_list


async def create_storages(
    *,
    netbox_session: object,
    pxs: list[object] | None,
    tag: object,
    websocket: object | None = None,
    use_websocket: bool = False,
    fetch_concurrency: int = 8,
) -> list[dict[str, object]]:
    """Create or update plugin storage rows for each Proxmox endpoint storage.

    The Proxmox /storage endpoint only returns storage IDs. For each storage,
    we must call /storage/{storage_id} to get full configuration including
    type, content, path, nodes, shared status, and enabled flag.
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

    async def _fetch_cluster_storage_ids(
        proxmox: object,
    ) -> tuple[str, list[str]]:
        cluster_name = str(getattr(proxmox, "name", "") or "").strip() or "unknown"
        async with fetch_sem:
            storage_items = await asyncio.to_thread(lambda: dump_models(get_storage_list(proxmox)))
            storage_ids = [s.get("storage") for s in storage_items if s.get("storage")]
        return cluster_name, storage_ids

    cluster_storage_ids = await asyncio.gather(
        *[_fetch_cluster_storage_ids(proxmox) for proxmox in pxs],
        return_exceptions=True,
    )

    async def _fetch_storage_config_for_cluster(
        cluster_name: str,
        storage_id: str,
    ) -> dict[str, object] | None:
        proxmox = proxmoxs_map.get(cluster_name)
        if not proxmox:
            return None
        async with fetch_sem:
            return await asyncio.to_thread(get_storage_config, proxmox, storage_id)

    tasks: list[asyncio.Task] = []
    cluster_storage_id_list: list[tuple[str, str]] = []

    for cluster_data in cluster_storage_ids:
        if isinstance(cluster_data, Exception):
            logger.warning("Error fetching storage list from Proxmox endpoint: %s", cluster_data)
            continue
        cluster_name, storage_ids = cluster_data
        for storage_id in storage_ids:
            cluster_storage_id_list.append((cluster_name, storage_id))
            tasks.append(
                asyncio.create_task(_fetch_storage_config_for_cluster(cluster_name, storage_id))
            )

    config_results = await asyncio.gather(*tasks, return_exceptions=True)

    storage_configs: list[tuple[str, str, dict[str, object]]] = []
    for (cluster_name, storage_id), result in zip(cluster_storage_id_list, config_results):
        if isinstance(result, Exception):
            logger.warning(
                "Error fetching storage config for %s/%s: %s",
                cluster_name,
                storage_id,
                result,
            )
            continue
        if result:
            storage_configs.append((cluster_name, storage_id, result))

    unique_payloads: dict[tuple[str, str], dict] = {}

    for cluster_name, storage_name, config in storage_configs:
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
