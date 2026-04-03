"""NetBox prerequisite records (sites, clusters, device shells) for Proxmox node sync."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
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


def _last_updated_cf() -> dict[str, str]:
    return {"proxmox_last_updated": datetime.now(timezone.utc).isoformat()}


def _record_has_tag(record: object, tag_slug: str) -> bool:
    if record is None:
        return False
    if hasattr(record, "serialize"):
        record_data = record.serialize()
    elif isinstance(record, dict):
        record_data = record
    else:
        record_data = {}

    tags = record_data.get("tags", [])
    if not isinstance(tags, list):
        return False

    return any(
        isinstance(tag, dict) and str(tag.get("slug") or "").strip() == tag_slug for tag in tags
    )


def _prefer_existing_device(records: list[object]) -> object | None:
    """Prefer the ProxBox-managed record when multiple same-name devices exist."""
    proxbox_records = [record for record in records if _record_has_tag(record, "proxbox")]
    if proxbox_records:
        return proxbox_records[0]
    return records[0] if records else None


async def _ensure_cluster_type(
    nb: object,
    *,
    mode: str,
    tag_refs: list[dict[str, object]],
) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/virtualization/cluster-types/",
        lookup={"slug": mode},
        payload={
            "name": mode.capitalize(),
            "slug": mode,
            "description": f"Proxmox {mode} mode",
            "tags": tag_refs,
            "custom_fields": _last_updated_cf(),
        },
        schema=NetBoxClusterTypeSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "description": record.get("description"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )


async def _ensure_cluster(
    nb: object,
    *,
    cluster_name: str,
    cluster_type_id: int | None,
    mode: str,
    tag_refs: list[dict[str, object]],
) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/virtualization/clusters/",
        lookup={"name": cluster_name},
        payload={
            "name": cluster_name,
            "type": cluster_type_id,
            "description": f"Proxmox {mode} cluster.",
            "tags": tag_refs,
            "custom_fields": _last_updated_cf(),
        },
        schema=NetBoxClusterSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "type": record.get("type"),
            "description": record.get("description"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )


async def _ensure_manufacturer(nb: object, *, tag_refs: list[dict[str, object]]) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/dcim/manufacturers/",
        lookup={"slug": "proxmox"},
        payload={
            "name": "Proxmox",
            "slug": "proxmox",
            "tags": tag_refs,
            "custom_fields": _last_updated_cf(),
        },
        schema=NetBoxManufacturerSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )


async def _ensure_device_type(
    nb: object,
    *,
    manufacturer_id: int | None,
    tag_refs: list[dict[str, object]],
) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/dcim/device-types/",
        lookup={"model": "Proxmox Generic Device"},
        payload={
            "model": "Proxmox Generic Device",
            "slug": "proxmox-generic-device",
            "manufacturer": manufacturer_id,
            "tags": tag_refs,
            "custom_fields": _last_updated_cf(),
        },
        schema=NetBoxDeviceTypeSyncState,
        current_normalizer=lambda record: {
            "model": record.get("model"),
            "slug": record.get("slug"),
            "manufacturer": record.get("manufacturer"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )


async def _ensure_device_role(nb: object, *, tag_refs: list[dict[str, object]]) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/dcim/device-roles/",
        lookup={"slug": "proxmox-node"},
        payload={
            "name": "Proxmox Node",
            "slug": "proxmox-node",
            "color": "00bcd4",
            "tags": tag_refs,
            "custom_fields": _last_updated_cf(),
        },
        schema=NetBoxDeviceRoleSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "color": record.get("color"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )


async def _ensure_site(
    nb: object, *, cluster_name: str, tag_refs: list[dict[str, object]]
) -> object:
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
            "custom_fields": _last_updated_cf(),
        },
        schema=NetBoxSiteSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "status": record.get("status"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )


async def _ensure_device(
    nb: object,
    *,
    device_name: str,
    cluster_id: int | None,
    device_type_id: int | None,
    role_id: int | None,
    site_id: int | None,
    tag_refs: list[dict[str, object]],
) -> object:
    existing_devices = await rest_list_async(
        nb,
        "/api/dcim/devices/",
        query={"name": device_name, "limit": 2},
    )
    existing_device = _prefer_existing_device(existing_devices)
    if existing_device is not None:
        existing_site = existing_device.get("site")
        if existing_site is not None and (site_id is None or existing_site != site_id):
            site_id = existing_site

    payload = {
        "name": device_name,
        "tags": tag_refs,
        "cluster": cluster_id,
        "status": "active",
        "description": f"Proxmox Node {device_name}",
        "device_type": device_type_id,
        "role": role_id,
        "site": site_id,
        "custom_fields": _last_updated_cf(),
    }

    if existing_device is not None:
        desired_model = NetBoxDeviceSyncState.model_validate(payload)
        desired_payload = desired_model.model_dump(exclude_none=True, by_alias=True)
        current_model = NetBoxDeviceSyncState.model_validate(
            {
                "name": existing_device.get("name"),
                "status": existing_device.get("status"),
                "cluster": existing_device.get("cluster"),
                "device_type": existing_device.get("device_type"),
                "role": existing_device.get("role"),
                "site": existing_device.get("site"),
                "description": existing_device.get("description"),
                "tags": existing_device.get("tags"),
                "custom_fields": existing_device.get("custom_fields"),
            }
        )
        current_payload = current_model.model_dump(exclude_none=True, by_alias=True)

        patch_payload = {
            key: value
            for key, value in desired_payload.items()
            if current_payload.get(key) != value
        }
        if patch_payload:
            for field, value in patch_payload.items():
                setattr(existing_device, field, value)
            await existing_device.save()
        return existing_device

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
            "custom_fields": record.get("custom_fields"),
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
