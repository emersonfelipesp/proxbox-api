"""Synchronize Proxmox storage definitions into NetBox plugin storage objects."""

from __future__ import annotations

from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxStorageSyncState
from proxbox_api.services.proxmox_helpers import dump_models, get_storage_list


async def create_storages(
    *,
    netbox_session,
    pxs,
    tag,
    websocket=None,
    use_websocket: bool = False,
) -> list[dict]:
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
    synced: list[dict] = []

    for proxmox in pxs:
        cluster_name = str(getattr(proxmox, "name", "") or "").strip() or "unknown"
        storages = dump_models(get_storage_list(proxmox))
        for storage in storages:
            storage_name = str(storage.get("storage") or "").strip()
            if not storage_name:
                continue

            payload = {
                "cluster": cluster_name,
                "name": storage_name,
                "storage_type": storage.get("type"),
                "content": storage.get("content"),
                "path": storage.get("path"),
                "nodes": storage.get("nodes"),
                "shared": bool(storage.get("shared")),
                "enabled": not bool(storage.get("disable")),
                "tags": tag_refs,
            }
            record = await rest_reconcile_async(
                nb,
                "/api/plugins/proxbox/storage/",
                lookup={"cluster": cluster_name, "name": storage_name},
                payload=payload,
                schema=NetBoxStorageSyncState,
                current_normalizer=lambda item: {
                    "cluster": item.get("cluster"),
                    "name": item.get("name"),
                    "storage_type": item.get("storage_type"),
                    "content": item.get("content"),
                    "path": item.get("path"),
                    "nodes": item.get("nodes"),
                    "shared": item.get("shared"),
                    "enabled": item.get("enabled"),
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
