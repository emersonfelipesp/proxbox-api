"""NetBox prerequisite records (sites, clusters, device shells) for Proxmox node sync."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from proxbox_api.constants import DISCOVERY_TAG_CLUSTER, DISCOVERY_TAG_NODE
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import (
    BulkReconcilePhase,
    rest_bulk_reconcile_phases_async,
    rest_first_async,
    rest_list_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxClusterSyncState,
    NetBoxClusterTypeSyncState,
    NetBoxDeviceRoleSyncState,
    NetBoxDeviceSyncState,
    NetBoxDeviceTypeSyncState,
    NetBoxManufacturerSyncState,
    NetBoxSiteSyncState,
)
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.discovery_tags import discovery_tag_ref, merge_tag_refs
from proxbox_api.types import NetBoxRecord


def _slugify(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "cluster"


def _last_updated_cf() -> dict[str, str]:
    return {"proxmox_last_updated": datetime.now(timezone.utc).isoformat()}


def _relation_id_or_none(value: object) -> int | None:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _relation_text_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _placement_raw_value(source: object | None, key: str) -> object:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def placement_from_source(source: object | None) -> dict[str, object | None]:
    """Extract endpoint placement metadata from a session, schema, dict, or nested relation."""

    placement: dict[str, object | None] = {}
    for prefix in ("site", "tenant"):
        nested = _placement_raw_value(source, prefix)
        nested_id = nested_slug = nested_name = None
        if isinstance(nested, dict):
            nested_id = nested.get("id")
            nested_slug = nested.get("slug")
            nested_name = nested.get("name") or nested.get("display")
        elif nested is not None:
            nested_id = getattr(nested, "id", None)
            nested_slug = getattr(nested, "slug", None)
            nested_name = getattr(nested, "name", None) or getattr(nested, "display", None)

        placement[f"{prefix}_id"] = _relation_id_or_none(
            _placement_raw_value(source, f"{prefix}_id") or nested_id
        )
        placement[f"{prefix}_slug"] = _relation_text_or_none(
            _placement_raw_value(source, f"{prefix}_slug") or nested_slug
        )
        placement[f"{prefix}_name"] = _relation_text_or_none(
            _placement_raw_value(source, f"{prefix}_name") or nested_name
        )
    return placement


def _has_configured_relation(placement: dict[str, object | None], prefix: str) -> bool:
    return any(
        placement.get(f"{prefix}_{field}") not in (None, "") for field in ("id", "slug", "name")
    )


async def _lookup_relation_record(
    nb: object,
    path: str,
    *,
    prefix: str,
    placement: dict[str, object | None],
) -> NetBoxRecord:
    attempts: list[tuple[str, object]] = []
    relation_id = _relation_id_or_none(placement.get(f"{prefix}_id"))
    if relation_id is not None:
        attempts.append(("id", relation_id))
    for field in ("slug", "name"):
        value = _relation_text_or_none(placement.get(f"{prefix}_{field}"))
        if value:
            attempts.append((field, value))

    for field, value in attempts:
        record = await rest_first_async(nb, path, query={field: value, "limit": 2})
        if record is not None:
            return record

    details = ", ".join(f"{field}={value!r}" for field, value in attempts) or "no lookup data"
    raise ProxboxException(
        message=f"Configured NetBox {prefix} was not found",
        detail=f"Could not resolve {prefix} for Proxmox endpoint placement ({details}).",
    )


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


def _prefer_existing_device(records: list[object]) -> NetBoxRecord | None:
    """Prefer the ProxBox-managed record when multiple same-name devices exist."""
    proxbox_records = [record for record in records if _record_has_tag(record, "proxbox")]
    if proxbox_records:
        return proxbox_records[0]
    return records[0] if records else None


def _existing_device_site_pin(
    existing_device: NetBoxRecord | None,
    desired_site_id: int | None,
) -> int | None:
    """Pin the device's existing site when present.

    NetBox enforces device names unique per site, so reusing the existing record's site
    avoids a unique-name conflict on update.
    """
    if existing_device is None:
        return desired_site_id
    existing_site = _relation_id_or_none(existing_device.get("site"))
    if existing_site is None:
        return desired_site_id
    if desired_site_id is None or existing_site != desired_site_id:
        return existing_site
    return desired_site_id


async def _resolve_existing_device_sites(
    nb: object,
    device_names: list[str],
) -> dict[str, int]:
    """Return a {device_name: existing_site_id} map for any matching ProxBox-managed devices."""
    pins: dict[str, int] = {}
    for device_name in device_names:
        try:
            existing = await rest_list_async(
                nb,
                "/api/dcim/devices/",
                query={"name": device_name, "limit": 2},
            )
        except Exception:
            continue
        record = _prefer_existing_device(existing)
        if record is None:
            continue
        site_id = _relation_id_or_none(record.get("site"))
        if site_id is not None:
            pins[device_name] = site_id
    return pins


def _cluster_type_payload(mode: str, tag_refs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": mode.capitalize(),
        "slug": mode,
        "description": f"Proxmox {mode} mode",
        "tags": tag_refs,
        "custom_fields": _last_updated_cf(),
    }


def _cluster_payload(
    cluster_name: str,
    *,
    cluster_type_id: int | None,
    mode: str,
    tag_refs: list[dict[str, object]],
    site_id: int | None = None,
    tenant_id: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": cluster_name,
        "type": cluster_type_id,
        "description": f"Proxmox {mode} cluster.",
        "tags": tag_refs,
        "custom_fields": _last_updated_cf(),
    }
    if site_id is not None:
        payload["scope_type"] = "dcim.site"
        payload["scope_id"] = site_id
    if tenant_id is not None:
        payload["tenant"] = tenant_id
    return payload


def _manufacturer_payload(tag_refs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": "Proxmox",
        "slug": "proxmox",
        "tags": tag_refs,
        "custom_fields": _last_updated_cf(),
    }


def _device_type_payload(
    manufacturer_id: int | None,
    tag_refs: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "model": "Proxmox Generic Device",
        "slug": "proxmox-generic-device",
        "manufacturer": manufacturer_id,
        "tags": tag_refs,
        "custom_fields": _last_updated_cf(),
    }


def _device_role_payload(tag_refs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": "Proxmox Node",
        "slug": "proxmox-node",
        "color": "00bcd4",
        "tags": tag_refs,
        "custom_fields": _last_updated_cf(),
    }


def _site_payload(cluster_name: str, tag_refs: list[dict[str, object]]) -> dict[str, object]:
    site_slug = f"proxmox-default-site-{_slugify(cluster_name)}"
    return {
        "name": f"Proxmox Default Site - {cluster_name}",
        "slug": site_slug,
        "status": "active",
        "tags": tag_refs,
        "custom_fields": _last_updated_cf(),
    }


def _device_payload(
    device_name: str,
    *,
    cluster_id: int | None,
    device_type_id: int | None,
    role_id: int | None,
    site_id: int | None,
    tag_refs: list[dict[str, object]],
) -> dict[str, object]:
    return {
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


def _device_selector(records: list[object]) -> NetBoxRecord | None:
    return _prefer_existing_device(records)


def _compute_device_patchable_fields(
    overwrite_flags: SyncOverwriteFlags | None,
    overwrite_device_role: bool,
    overwrite_device_type: bool,
    overwrite_device_tags: bool,
) -> set[str]:
    """Build the patchable_fields allowlist for Proxmox node devices.

    site is intentionally excluded (moving a device between sites violates the
    unique-per-site name constraint). cluster is always patchable so devices
    follow node-to-cluster reassignment. Used by both ensure_proxmox_devices_bulk
    (DCIM sync) and _ensure_device (per-VM parent-device materialization), so the
    flag enforcement stays identical across both write paths.
    """
    fields: set[str] = {"cluster"}
    if overwrite_flags is None or overwrite_flags.overwrite_device_status:
        fields.add("status")
    if overwrite_flags is None or overwrite_flags.overwrite_device_description:
        fields.add("description")
    if overwrite_flags is None or overwrite_flags.overwrite_device_custom_fields:
        fields.add("custom_fields")
    if overwrite_device_role:
        fields.add("role")
    if overwrite_device_type:
        fields.add("device_type")
    if overwrite_device_tags:
        fields.add("tags")
    return fields


async def ensure_proxmox_devices_bulk(  # noqa: C901
    nb: object,
    *,
    clusters_status: list[object] | None,
    tag_refs: list[dict[str, object]],
    overwrite_device_role: bool = True,
    overwrite_device_type: bool = True,
    overwrite_device_tags: bool = True,
    overwrite_flags: SyncOverwriteFlags | None = None,
) -> dict[str, NetBoxRecord]:
    """Create/update Proxmox prerequisite NetBox objects in dependency order."""
    if not clusters_status:
        return {}

    cluster_modes: dict[str, str] = {}
    cluster_placements: dict[str, dict[str, object | None]] = {}
    node_names: list[str] = []
    for cluster_status in clusters_status:
        cluster_name = str(getattr(cluster_status, "name", "") or "").strip()
        cluster_mode = str(getattr(cluster_status, "mode", "") or "").strip().lower() or "cluster"
        if cluster_name:
            cluster_modes[cluster_name] = cluster_mode
            cluster_placements[cluster_name] = placement_from_source(cluster_status)
        for node in getattr(cluster_status, "node_list", None) or []:
            node_name = str(getattr(node, "name", "") or "").strip()
            if node_name:
                node_names.append(node_name)

    if not cluster_modes and not node_names:
        return {}

    default_site_cluster_names = [
        cluster_name
        for cluster_name in sorted(cluster_modes)
        if not _has_configured_relation(cluster_placements.get(cluster_name, {}), "site")
    ]

    phase_results = await rest_bulk_reconcile_phases_async(
        nb,
        [
            BulkReconcilePhase(
                name="cluster_types",
                path="/api/virtualization/cluster-types/",
                payloads=[
                    _cluster_type_payload(mode, tag_refs)
                    for mode in sorted(set(cluster_modes.values()))
                ],
                lookup_fields=["slug"],
                schema=NetBoxClusterTypeSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "description": record.get("description"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            ),
            BulkReconcilePhase(
                name="manufacturers",
                path="/api/dcim/manufacturers/",
                payloads=[_manufacturer_payload(tag_refs)],
                lookup_fields=["slug"],
                schema=NetBoxManufacturerSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            ),
            BulkReconcilePhase(
                name="device_roles",
                path="/api/dcim/device-roles/",
                payloads=[_device_role_payload(tag_refs)],
                lookup_fields=["slug"],
                schema=NetBoxDeviceRoleSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "color": record.get("color"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            ),
            BulkReconcilePhase(
                name="sites",
                path="/api/dcim/sites/",
                payloads=[
                    _site_payload(cluster_name, tag_refs)
                    for cluster_name in default_site_cluster_names
                ],
                lookup_fields=["slug"],
                schema=NetBoxSiteSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "status": record.get("status"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            ),
        ],
    )

    cluster_type_by_slug = {
        str(record.get("slug")): record for record in phase_results["cluster_types"].records
    }
    manufacturer = (
        phase_results["manufacturers"].records[0]
        if phase_results["manufacturers"].records
        else None
    )
    role = (
        phase_results["device_roles"].records[0] if phase_results["device_roles"].records else None
    )
    site_by_slug = {str(record.get("slug")): record for record in phase_results["sites"].records}
    site_by_cluster_name: dict[str, NetBoxRecord] = {}
    tenant_by_cluster_name: dict[str, NetBoxRecord] = {}
    for cluster_name in sorted(cluster_modes):
        placement = cluster_placements.get(cluster_name, {})
        site_slug = f"proxmox-default-site-{_slugify(cluster_name)}"
        site_record = site_by_slug.get(site_slug)
        if site_record is None:
            site_record = await _ensure_site(
                nb,
                cluster_name=cluster_name,
                tag_refs=tag_refs,
                placement=placement,
            )
        site_by_cluster_name[cluster_name] = site_record

        tenant_record = await _resolve_tenant(nb, placement=placement)
        if tenant_record is not None:
            tenant_by_cluster_name[cluster_name] = tenant_record

    # Cluster reconcile honors per-field cluster overwrite flags. name/type are
    # always patchable (they identify the cluster and its mode); description,
    # tags, and custom_fields are gated by the corresponding flags. When no
    # flags are supplied (None), all five keys are patchable, preserving the
    # historical always-overwrite behavior.
    _cluster_patchable: set[str] = {"name", "type"}
    _cluster_patchable.update({"scope_type", "scope_id", "tenant"})
    if overwrite_flags is None or overwrite_flags.overwrite_cluster_description:
        _cluster_patchable.add("description")
    if overwrite_flags is None or overwrite_flags.overwrite_cluster_tags:
        _cluster_patchable.add("tags")
    if overwrite_flags is None or overwrite_flags.overwrite_cluster_custom_fields:
        _cluster_patchable.add("custom_fields")

    dependency_phase_results = await rest_bulk_reconcile_phases_async(
        nb,
        [
            BulkReconcilePhase(
                name="clusters",
                path="/api/virtualization/clusters/",
                payloads=[
                    _cluster_payload(
                        cluster_name,
                        cluster_type_id=_relation_id_or_none(
                            cluster_type_by_slug[cluster_modes[cluster_name]].get("id")
                            if cluster_modes[cluster_name] in cluster_type_by_slug
                            else None
                        ),
                        mode=cluster_modes[cluster_name],
                        tag_refs=tag_refs,
                        site_id=_relation_id_or_none(
                            getattr(site_by_cluster_name.get(cluster_name), "id", None)
                        ),
                        tenant_id=_relation_id_or_none(
                            getattr(tenant_by_cluster_name.get(cluster_name), "id", None)
                        ),
                    )
                    for cluster_name in sorted(cluster_modes)
                ],
                lookup_fields=["name"],
                schema=NetBoxClusterSyncState,
                patchable_fields=frozenset(_cluster_patchable),
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "type": _relation_id_or_none(record.get("type")),
                    "tenant": _relation_id_or_none(record.get("tenant")),
                    "scope_type": record.get("scope_type"),
                    "scope_id": _relation_id_or_none(record.get("scope_id") or record.get("scope")),
                    "description": record.get("description"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            ),
            BulkReconcilePhase(
                name="device_types",
                path="/api/dcim/device-types/",
                payloads=[
                    _device_type_payload(
                        _relation_id_or_none(getattr(manufacturer, "id", None)), tag_refs
                    )
                ],
                lookup_fields=["model"],
                schema=NetBoxDeviceTypeSyncState,
                current_normalizer=lambda record: {
                    "model": record.get("model"),
                    "slug": record.get("slug"),
                    "manufacturer": _relation_id_or_none(record.get("manufacturer")),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            ),
        ],
    )

    cluster_by_name = {
        str(record.get("name")): record for record in dependency_phase_results["clusters"].records
    }
    device_type = (
        dependency_phase_results["device_types"].records[0]
        if dependency_phase_results["device_types"].records
        else None
    )

    existing_site_pins = await _resolve_existing_device_sites(nb, sorted(set(node_names)))

    device_payloads: list[dict[str, object]] = []
    for cluster_status in clusters_status:
        cluster_name = str(getattr(cluster_status, "name", "") or "").strip()
        cluster_record = cluster_by_name.get(cluster_name)
        site_record = site_by_cluster_name.get(cluster_name)
        desired_site_id = _relation_id_or_none(getattr(site_record, "id", None))
        for node in getattr(cluster_status, "node_list", None) or []:
            node_name = str(getattr(node, "name", "") or "").strip()
            if not node_name:
                continue
            site_id = existing_site_pins.get(node_name, desired_site_id)
            device_payloads.append(
                _device_payload(
                    node_name,
                    cluster_id=_relation_id_or_none(getattr(cluster_record, "id", None)),
                    device_type_id=_relation_id_or_none(getattr(device_type, "id", None)),
                    role_id=_relation_id_or_none(getattr(role, "id", None)),
                    site_id=site_id,
                    tag_refs=tag_refs,
                )
            )

    _device_patchable = _compute_device_patchable_fields(
        overwrite_flags,
        overwrite_device_role,
        overwrite_device_type,
        overwrite_device_tags,
    )

    device_results = await rest_bulk_reconcile_phases_async(
        nb,
        [
            BulkReconcilePhase(
                name="devices",
                path="/api/dcim/devices/",
                payloads=device_payloads,
                lookup_fields=["name"],
                schema=NetBoxDeviceSyncState,
                patchable_fields=frozenset(_device_patchable),
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "status": record.get("status"),
                    "cluster": _relation_id_or_none(record.get("cluster")),
                    "device_type": _relation_id_or_none(record.get("device_type")),
                    "role": _relation_id_or_none(record.get("role")),
                    "site": _relation_id_or_none(record.get("site")),
                    "description": record.get("description"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
                selector=_device_selector,
            )
        ],
    )

    devices = {str(record.get("name")): record for record in device_results["devices"].records}
    return devices


async def _ensure_cluster_type(
    nb: object,
    *,
    mode: str,
    tag_refs: list[dict[str, object]],
) -> NetBoxRecord:
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
    site_id: int | None = None,
    tenant_id: int | None = None,
) -> NetBoxRecord:
    # Pre-check existence so the first-discovery audit tag (issue #362) only
    # lands in the create payload. On update we merge with the current tag
    # set to keep the discovery slug and any operator-added tags intact.
    existing_cluster = await rest_first_async(
        nb,
        "/api/virtualization/clusters/",
        query={"name": cluster_name},
    )
    if existing_cluster is None:
        effective_tag_refs: list[dict[str, object]] = [
            *tag_refs,
            discovery_tag_ref(DISCOVERY_TAG_CLUSTER),
        ]
    else:
        existing_tags = (
            existing_cluster.serialize().get("tags")
            if hasattr(existing_cluster, "serialize")
            else None
        )
        effective_tag_refs = merge_tag_refs(list(tag_refs), existing_tags)

    return await rest_reconcile_async(
        nb,
        "/api/virtualization/clusters/",
        lookup={"name": cluster_name},
        payload=_cluster_payload(
            cluster_name,
            cluster_type_id=cluster_type_id,
            mode=mode,
            tag_refs=effective_tag_refs,
            site_id=site_id,
            tenant_id=tenant_id,
        ),
        schema=NetBoxClusterSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "type": _relation_id_or_none(record.get("type")),
            "tenant": _relation_id_or_none(record.get("tenant")),
            "scope_type": record.get("scope_type"),
            "scope_id": _relation_id_or_none(record.get("scope_id") or record.get("scope")),
            "description": record.get("description"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )


async def _ensure_manufacturer(nb: object, *, tag_refs: list[dict[str, object]]) -> NetBoxRecord:
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
) -> NetBoxRecord:
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


async def _ensure_device_role(nb: object, *, tag_refs: list[dict[str, object]]) -> NetBoxRecord:
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
    nb: object,
    *,
    cluster_name: str,
    tag_refs: list[dict[str, object]],
    placement: object | None = None,
) -> NetBoxRecord:
    placement_data = placement_from_source(placement)
    if _has_configured_relation(placement_data, "site"):
        return await _lookup_relation_record(
            nb,
            "/api/dcim/sites/",
            prefix="site",
            placement=placement_data,
        )

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


async def _resolve_tenant(
    nb: object,
    *,
    placement: object | None = None,
) -> NetBoxRecord | None:
    placement_data = placement_from_source(placement)
    if not _has_configured_relation(placement_data, "tenant"):
        return None
    return await _lookup_relation_record(
        nb,
        "/api/tenancy/tenants/",
        prefix="tenant",
        placement=placement_data,
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
    overwrite_device_role: bool = True,
    overwrite_device_type: bool = True,
    overwrite_device_tags: bool = True,
    overwrite_flags: SyncOverwriteFlags | None = None,
) -> NetBoxRecord:
    existing_devices = await rest_list_async(
        nb,
        "/api/dcim/devices/",
        query={"name": device_name, "limit": 2},
    )
    existing_device = _prefer_existing_device(existing_devices)
    site_id = _existing_device_site_pin(existing_device, site_id)

    # First-discovery audit tag (issue #362). Stamp the node-discovery slug
    # in the create payload only; on update, merge the desired tag refs with
    # whatever the existing device already carries so neither the discovery
    # tag nor operator-added tags get stripped.
    if existing_device is None:
        effective_tag_refs: list[dict[str, object]] = [
            *tag_refs,
            discovery_tag_ref(DISCOVERY_TAG_NODE),
        ]
    else:
        effective_tag_refs = merge_tag_refs(
            list(tag_refs),
            existing_device.get("tags"),
        )

    payload = {
        "name": device_name,
        "tags": effective_tag_refs,
        "cluster": cluster_id,
        "status": "active",
        "description": f"Proxmox Node {device_name}",
        "device_type": device_type_id,
        "role": role_id,
        "site": site_id,
        "custom_fields": _last_updated_cf(),
    }

    allowed = _compute_device_patchable_fields(
        overwrite_flags,
        overwrite_device_role,
        overwrite_device_type,
        overwrite_device_tags,
    )

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
            if current_payload.get(key) != value and key in allowed
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
        patchable_fields=frozenset(allowed),
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
    """Wrap a device sync phase error in ProxboxException with context.

    Args:
        phase: The phase name (e.g., "device_type", "cluster").
        error: The original exception.

    Returns:
        ProxboxException with context about the failed phase.
    """
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
