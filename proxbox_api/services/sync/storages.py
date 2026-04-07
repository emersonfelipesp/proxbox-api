"""Synchronize Proxmox storage definitions into NetBox plugin storage objects."""

from __future__ import annotations

import asyncio
import inspect

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxStorageSyncState
from proxbox_api.schemas.stream_messages import ItemOperation
from proxbox_api.services.proxmox_helpers import dump_models, get_storage_config, get_storage_list
from proxbox_api.utils.streaming import WebSocketSSEBridge


async def create_storages(  # noqa: C901
    *,
    netbox_session: object,
    pxs: list[object] | None,
    tag: object,
    websocket: object | None = None,
    use_websocket: bool = False,
    fetch_concurrency: int = 8,
) -> list[dict[str, object]]:
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
    synced: list[dict[str, object]] = []

    if not pxs:
        logger.info("No Proxmox sessions available for storage sync")
        return synced

    bridge: WebSocketSSEBridge | None = (
        websocket if isinstance(websocket, WebSocketSSEBridge) else None
    )
    fetch_sem = asyncio.Semaphore(max(1, int(fetch_concurrency)))

    cluster_id_map: dict[str, int] = {}
    try:
        clusters = await rest_list_async(nb, "/api/virtualization/clusters/")
        for cluster in clusters:
            cluster_name = str(cluster.get("name", "")).strip()
            cluster_id = cluster.get("id")
            if cluster_name and cluster_id:
                cluster_id_map[cluster_name] = int(cluster_id)
    except Exception as error:
        logger.warning("Unable to prefetch NetBox clusters for storage sync: %s", error)

    proxmoxs_map = {
        str(getattr(px, "name", "") or "").strip(): px
        for px in pxs or []
        if str(getattr(px, "name", "") or "").strip()
    }

    async def _fetch_cluster_storage_items(proxmox: object) -> tuple[str, list[dict[str, object]]]:
        cluster_name = str(getattr(proxmox, "name", "") or "").strip() or "unknown"
        async with fetch_sem:
            storage_items = get_storage_list(proxmox)
            if inspect.isawaitable(storage_items):
                storage_items = await storage_items
            return cluster_name, dump_models(storage_items)

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
                        fetched_config = get_storage_config(proxmox, storage_name)
                        if inspect.isawaitable(fetched_config):
                            fetched_config = await fetched_config
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

    if bridge:
        await bridge.emit_discovery(
            phase="storage",
            items=[
                {
                    "name": storage_name,
                    "type": "storage",
                    "cluster": cluster_name,
                    "node": str(config.get("nodes") or ""),
                }
                for (cluster_name, storage_name), config in unique_payloads.items()
            ],
            message=f"Discovered {len(unique_payloads)} storage item(s) to synchronize",
            metadata={"fetch_concurrency": int(fetch_concurrency)},
        )

    total_items = len(unique_payloads)
    skipped_count = 0
    storage_payloads: list[dict[str, object]] = []
    ordered_items: list[tuple[str, str]] = []

    for (cluster_name, storage_name), config in unique_payloads.items():
        cluster_id = cluster_id_map.get(cluster_name)
        if cluster_id is None:
            skipped_count += 1
            if bridge:
                await bridge.emit_item_progress(
                    phase="storage",
                    item={"name": storage_name, "type": "storage", "cluster": cluster_name},
                    operation=ItemOperation.SKIPPED,
                    status="skipped",
                    message=(
                        f"Skipped storage '{cluster_name}/{storage_name}' "
                        "because cluster was not found in NetBox"
                    ),
                    progress_current=skipped_count,
                    progress_total=total_items,
                    warning="Cluster not found in NetBox",
                )
            continue

        storage_payloads.append(
            {
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
        )
        ordered_items.append((cluster_name, storage_name))

    failed_count = 0
    processed_count = skipped_count
    for index, (cluster_name, storage_name) in enumerate(ordered_items):
        processed_count += 1
        payload = storage_payloads[index]
        lookup = {"cluster": payload["cluster"], "name": payload["name"]}

        try:
            record = await rest_reconcile_async(
                nb,
                "/api/plugins/proxbox/storage/",
                lookup=lookup,
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
        except Exception:
            failed_count += 1
            if bridge:
                await bridge.emit_item_progress(
                    phase="storage",
                    item={"name": storage_name, "type": "storage", "cluster": cluster_name},
                    operation=ItemOperation.FAILED,
                    status="failed",
                    message=f"Failed to sync storage '{cluster_name}/{storage_name}'",
                    progress_current=processed_count,
                    progress_total=total_items,
                    error="Storage missing from bulk reconcile result",
                )
            continue

        synced.append(data)
        if bridge:
            await bridge.emit_item_progress(
                phase="storage",
                item={
                    "name": storage_name,
                    "type": "storage",
                    "cluster": cluster_name,
                    "netbox_id": data.get("id"),
                    "netbox_url": data.get("url") or data.get("display_url"),
                },
                operation=ItemOperation.UPDATED,
                status="completed",
                message=f"Synced storage '{cluster_name}/{storage_name}'",
                progress_current=processed_count,
                progress_total=total_items,
            )
        elif use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "storage",
                    "type": "sync",
                    "data": {
                        "rowid": f"{cluster_name}/{storage_name}",
                        "name": storage_name,
                        "completed": True,
                        "status": "synced",
                        "message": f"Synced storage {cluster_name}/{storage_name}",
                        "result": {"id": data.get("id"), "name": storage_name},
                    },
                }
            )

    if bridge:
        await bridge.emit_phase_summary(
            phase="storage",
            created=0,
            updated=len(synced),
            failed=failed_count,
            skipped=skipped_count,
            message=(
                f"Storage synchronization completed: {len(synced)} synced, {failed_count} failed"
            ),
        )

    return synced
