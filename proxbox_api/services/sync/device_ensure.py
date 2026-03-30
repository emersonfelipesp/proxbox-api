"""NetBox prerequisite records (sites, clusters, device shells) for Proxmox node sync."""

import re

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxClusterSyncState,
    NetBoxClusterTypeSyncState,
    NetBoxDeviceRoleSyncState,
    NetBoxDeviceSyncState,
    NetBoxDeviceTypeSyncState,
    NetBoxManufacturerSyncState,
    NetBoxSiteSyncState,
)


def _slugify(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "cluster"


async def _ensure_cluster_type(nb, *, mode: str, tag_refs: list[dict]) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/virtualization/cluster-types/",
        lookup={"slug": mode},
        payload={
            "name": mode.capitalize(),
            "slug": mode,
            "description": f"Proxmox {mode} mode",
            "tags": tag_refs,
        },
        schema=NetBoxClusterTypeSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "description": record.get("description"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_cluster(
    nb, *, cluster_name: str, cluster_type_id: int | None, mode: str, tag_refs
):
    return await rest_reconcile_async(
        nb,
        "/api/virtualization/clusters/",
        lookup={"name": cluster_name},
        payload={
            "name": cluster_name,
            "type": cluster_type_id,
            "description": f"Proxmox {mode} cluster.",
            "tags": tag_refs,
        },
        schema=NetBoxClusterSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "type": record.get("type"),
            "description": record.get("description"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_manufacturer(nb, *, tag_refs: list[dict]) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/dcim/manufacturers/",
        lookup={"slug": "proxmox"},
        payload={
            "name": "Proxmox",
            "slug": "proxmox",
            "tags": tag_refs,
        },
        schema=NetBoxManufacturerSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_device_type(nb, *, manufacturer_id: int | None, tag_refs: list[dict]) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/dcim/device-types/",
        lookup={"model": "Proxmox Generic Device"},
        payload={
            "model": "Proxmox Generic Device",
            "slug": "proxmox-generic-device",
            "manufacturer": manufacturer_id,
            "tags": tag_refs,
        },
        schema=NetBoxDeviceTypeSyncState,
        current_normalizer=lambda record: {
            "model": record.get("model"),
            "slug": record.get("slug"),
            "manufacturer": record.get("manufacturer"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_device_role(nb, *, tag_refs: list[dict]) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/dcim/device-roles/",
        lookup={"slug": "proxmox-node"},
        payload={
            "name": "Proxmox Node",
            "slug": "proxmox-node",
            "color": "00bcd4",
            "tags": tag_refs,
        },
        schema=NetBoxDeviceRoleSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "color": record.get("color"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_site(nb, *, cluster_name: str, tag_refs: list[dict]) -> object:
    site_name = f"Proxmox Default Site - {cluster_name}"
    site_slug = f"proxmox-default-site-{_slugify(cluster_name)}"
    return await rest_reconcile_async(
        nb,
        "/api/dcim/sites/",
        lookup={"slug": site_slug},
        payload={
            "name": site_name,
            "slug": site_slug,
            "status": "active",
            "tags": tag_refs,
        },
        schema=NetBoxSiteSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "status": record.get("status"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_device(
    nb,
    *,
    device_name: str,
    cluster_id: int | None,
    device_type_id: int | None,
    role_id: int | None,
    site_id: int | None,
    tag_refs: list[dict],
) -> object:
    payload = {
        "name": device_name,
        "tags": tag_refs,
        "cluster": cluster_id,
        "status": "active",
        "description": f"Proxmox Node {device_name}",
        "device_type": device_type_id,
        "role": role_id,
        "site": site_id,
    }
    return await rest_reconcile_async(
        nb,
        "/api/dcim/devices/",
        lookup={"name": device_name, "site_id": site_id},
        payload=payload,
        schema=NetBoxDeviceSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "status": record.get("status"),
            "cluster": record.get("cluster"),
            "device_type": record.get("device_type"),
            "role": record.get("role"),
            "site": record.get("site"),
            "description": record.get("description"),
            "tags": record.get("tags"),
        },
    )


def _wrap_device_phase_error(phase: str, error: Exception) -> ProxboxException:
    if isinstance(error, ProxboxException):
        return ProxboxException(
            message=f"Error creating NetBox {phase}",
            detail=error.detail or error.message,
            python_exception=error.python_exception,
        )
    return ProxboxException(
        message=f"Error creating NetBox {phase}",
        detail=str(error),
        python_exception=str(error),
    )
