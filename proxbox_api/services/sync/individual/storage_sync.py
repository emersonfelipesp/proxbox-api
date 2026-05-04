"""Individual Storage sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxStorageSyncState
from proxbox_api.services.proxmox_helpers import get_storage_config_individual
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService


async def sync_storage_individual(
    nb: object,
    px: object,
    tag: object,
    cluster_name: str,
    storage_name: str,
    dry_run: bool = False,
) -> dict:
    """Sync a single Storage from Proxmox to NetBox.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        cluster_name: Name of the cluster.
        storage_name: Name of the storage to sync.
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict.
    """
    service = BaseIndividualSyncService(nb, px, tag)
    tag_refs = service.tag_refs
    now = datetime.now(timezone.utc)

    try:
        storage_config = get_storage_config_individual(px, storage_name)
    except Exception:
        storage_config = {}

    proxmox_resource: dict[str, object] = {
        "storage": storage_name,
        "cluster": cluster_name,
        "config": storage_config,
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        existing = await rest_list_async(
            nb,
            "/api/plugins/proxbox/storage/",
            query={"name": storage_name},
        )
        netbox_object = None
        if existing:
            netbox_object = existing[0].serialize() if hasattr(existing[0], "serialize") else None

        cluster_dep: dict[str, object] = {"object_type": "cluster", "name": cluster_name}
        return {
            "object_type": "storage",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": [cluster_dep],
            "error": None,
        }

    try:
        cluster = await service._get_or_create_cluster(cluster_name)
        cluster_id = int(getattr(cluster, "id", 0) or 0)

        storage_type = storage_config.get("type", "unknown")
        content = storage_config.get("content", None)
        path = storage_config.get("path", None)
        nodes = storage_config.get("nodes", None)
        shared = storage_config.get("shared", 0)
        disable = storage_config.get("disable", 0)

        if isinstance(shared, str):
            shared = shared.lower() in ("1", "true", "yes")
        else:
            shared = bool(shared)

        if isinstance(disable, str):
            enabled = disable.lower() not in ("1", "true", "yes")
        else:
            enabled = not bool(disable)

        storage_payload: dict[str, object] = {
            "cluster": cluster_id,
            "name": storage_name,
            "storage_type": storage_type,
            "content": content,
            "path": path,
            "nodes": nodes,
            "shared": shared,
            "enabled": enabled,
            "server": storage_config.get("server"),
            "port": storage_config.get("port"),
            "username": storage_config.get("username"),
            "export": storage_config.get("export"),
            "share": storage_config.get("share"),
            "pool": storage_config.get("pool"),
            "monhost": storage_config.get("monhost"),
            "namespace": storage_config.get("namespace"),
            "datastore": storage_config.get("datastore"),
            "subdir": storage_config.get("subdir"),
            "mountpoint": storage_config.get("mountpoint"),
            "is_mountpoint": storage_config.get("is_mountpoint"),
            "preallocation": storage_config.get("preallocation"),
            "format": storage_config.get("format"),
            "prune_backups": storage_config.get("prune-backups"),
            "max_protected_backups": storage_config.get("max-protected-backups"),
            "raw_config": storage_config,
            "tags": tag_refs,
        }

        existing_storages = await rest_list_async(
            nb,
            "/api/plugins/proxbox/storage/",
            query={"cluster": cluster_id, "name": storage_name},
        )
        storage_record = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/storage/",
            lookup={"cluster": cluster_id, "name": storage_name},
            payload=storage_payload,
            schema=NetBoxStorageSyncState,
            current_normalizer=lambda record: {
                "cluster": record.get("cluster", {}).get("id")
                if isinstance(record.get("cluster"), dict)
                else record.get("cluster"),
                "name": record.get("name"),
                "storage_type": record.get("storage_type"),
                "content": record.get("content"),
                "path": record.get("path"),
                "nodes": record.get("nodes"),
                "shared": record.get("shared"),
                "enabled": record.get("enabled"),
                "server": record.get("server"),
                "port": record.get("port"),
                "username": record.get("username"),
                "export": record.get("export"),
                "share": record.get("share"),
                "pool": record.get("pool"),
                "monhost": record.get("monhost"),
                "namespace": record.get("namespace"),
                "datastore": record.get("datastore"),
                "subdir": record.get("subdir"),
                "mountpoint": record.get("mountpoint"),
                "is_mountpoint": record.get("is_mountpoint"),
                "preallocation": record.get("preallocation"),
                "format": record.get("format"),
                "prune_backups": record.get("prune_backups"),
                "max_protected_backups": record.get("max_protected_backups"),
                "raw_config": record.get("raw_config"),
                "tags": record.get("tags"),
            },
        )

        netbox_object = storage_record.serialize() if hasattr(storage_record, "serialize") else None
        action = "updated" if existing_storages else "created"

        return {
            "object_type": "storage",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": [
                {"object_type": "cluster", "name": cluster_name, "action": action}
            ],
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "storage",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }
