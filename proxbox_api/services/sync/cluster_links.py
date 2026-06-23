"""Helpers for linking plugin ProxmoxCluster rows to NetBox clusters."""

from __future__ import annotations

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async, rest_patch_async
from proxbox_api.services.sync.vm_helpers import (
    record_id,
    resolve_netbox_cluster_id_by_name,
)

PROXMOX_CLUSTER_PATH = "/api/plugins/proxbox/proxmox-clusters/"


def _record_field(record: object, key: str) -> object:
    if isinstance(record, dict):
        return record.get(key)
    getter = getattr(record, "get", None)
    if callable(getter):
        return getter(key)
    return getattr(record, key, None)


async def sync_proxmox_cluster_netbox_link(nb: object, *, cluster_name: str) -> int:
    """Set ``ProxmoxCluster.netbox_cluster`` for plugin rows matching a cluster name.

    The NetBox cluster is resolved by the same exact-name helper VM and
    dependent-object sync already use. Every plugin row with the matching name
    is repaired, which covers multi-endpoint Proxmox clusters that have one
    ``ProxmoxCluster`` row per endpoint.
    """

    normalized_name = str(cluster_name or "").strip()
    if not normalized_name:
        return 0

    netbox_cluster_id = await resolve_netbox_cluster_id_by_name(nb, normalized_name)
    if netbox_cluster_id is None:
        return 0

    try:
        proxmox_clusters = await rest_list_async(
            nb,
            PROXMOX_CLUSTER_PATH,
            query={"name": normalized_name},
        )
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "Could not list ProxmoxCluster rows for cluster %s: %s",
            normalized_name,
            error,
        )
        return 0

    updated = 0
    cache_key = normalized_name.casefold()
    for proxmox_cluster in proxmox_clusters:
        row_name = str(_record_field(proxmox_cluster, "name") or "").strip()
        if row_name.casefold() != cache_key:
            continue

        current_cluster_id = record_id(_record_field(proxmox_cluster, "netbox_cluster"))
        if current_cluster_id == netbox_cluster_id:
            continue

        proxmox_cluster_id = record_id(proxmox_cluster)
        if proxmox_cluster_id is None:
            continue

        try:
            await rest_patch_async(
                nb,
                PROXMOX_CLUSTER_PATH,
                proxmox_cluster_id,
                {"netbox_cluster": netbox_cluster_id},
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Could not link ProxmoxCluster row %s to NetBox cluster %s: %s",
                proxmox_cluster_id,
                netbox_cluster_id,
                error,
            )
            continue
        updated += 1

    return updated
