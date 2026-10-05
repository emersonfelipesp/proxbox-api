"""NetBox prerequisite records (sites, clusters, device shells) for Proxmox node sync."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from proxbox_api.constants import DISCOVERY_TAG_CLUSTER, DISCOVERY_TAG_NODE, PROXBOX_TAG
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
from proxbox_api.services.sync.cluster_links import sync_proxmox_cluster_netbox_link
from proxbox_api.services.sync.discovery_tags import (
    discovery_tag_ref,
    merge_tag_refs,
    resolve_discovery_tag_id,
)
from proxbox_api.services.sync.node_device_name import NodeDeviceNameError, render_node_device_name
from proxbox_api.services.sync.sync_state_writer import (
    write_cluster_sync_state,
    write_device_sync_state,
)
from proxbox_api.types import NetBoxRecord

logger = logging.getLogger(__name__)


def _slugify(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "cluster"


def _payload_last_updated(payload: dict[str, object]) -> object:
    """Return a fresh timestamp for a typed sync-state sidecar write."""
    return datetime.now(timezone.utc).isoformat()


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


def _record_value(source: object | None, key: str) -> object:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(key)
    getter = getattr(source, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except TypeError:
            pass
    return getattr(source, key, None)


def _scope_type_is_site(value: object) -> bool:
    if isinstance(value, dict):
        object_type = str(value.get("object_type") or value.get("value") or "").strip().lower()
        app_label = str(value.get("app_label") or "").strip().lower()
        model = str(value.get("model") or "").strip().lower()
        if object_type == "dcim.site" or object_type.endswith(".site"):
            return True
        return app_label == "dcim" and model == "site"

    text = str(value or "").strip().lower()
    return text in {"dcim.site", "site"} or text.endswith(".site")


def _scope_value_is_site(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if _scope_type_is_site(
        value.get("object_type") or value.get("type") or value.get("content_type")
    ):
        return True
    url = str(value.get("url") or "").strip().lower()
    return "/api/dcim/sites/" in url


def _effective_cluster_site_id(
    cluster_record: object | None,
    *,
    fallback_site_id: object | None = None,
) -> int | None:
    """Return the site NetBox will enforce for devices/VMs assigned to a cluster."""

    fallback = _relation_id_or_none(fallback_site_id)
    scope_value = _record_value(cluster_record, "scope")
    scope_id = _relation_id_or_none(_record_value(cluster_record, "scope_id"))
    if scope_id is None:
        scope_id = _relation_id_or_none(scope_value)
    if scope_id is None:
        return fallback

    scope_type = _record_value(cluster_record, "scope_type")
    if _scope_type_is_site(scope_type) or _scope_value_is_site(scope_value):
        return scope_id
    return fallback


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


def _ordered_device_candidates(records: list[object]) -> list[NetBoxRecord]:
    """Return candidates with ProxBox-managed records ahead of manual records."""
    proxbox_records = [record for record in records if _record_has_tag(record, "proxbox")]
    if not proxbox_records:
        return list(records)
    proxbox_ids = {id(record) for record in proxbox_records}
    return [*proxbox_records, *[record for record in records if id(record) not in proxbox_ids]]


def _first_proxbox_tagged(records: list[object]) -> NetBoxRecord | None:
    """Return the first record carrying the ``proxbox`` tag, else ``None``."""
    for record in records:
        if _record_has_tag(record, PROXBOX_TAG):
            return record
    return None


def _select_existing_device_for_target(
    records: list[object],
    *,
    desired_site_id: int | None,
    cluster_id: int | None,
) -> NetBoxRecord | None:
    """Select an existing device when it is compatible with the target placement.

    A same-name device is adopted when it already sits in the target site or
    cluster. When neither matches, an existing device is reused only if the
    operator has explicitly opted in by assigning the ``proxbox`` tag to it
    (issue #561): tagging a pre-existing Device flags it as adoptable, so
    Proxbox attaches it to the Proxmox cluster (pinned to its existing site via
    ``_existing_device_site_pin``) instead of creating a duplicate. Untagged
    same-name devices in a different site/cluster are never silently adopted.
    """
    candidates = _ordered_device_candidates(records)
    if not candidates:
        return None

    if desired_site_id is not None:
        for record in candidates:
            if _relation_id_or_none(record.get("site")) == desired_site_id:
                return record

    if cluster_id is not None:
        for record in candidates:
            if _relation_id_or_none(record.get("cluster")) == cluster_id:
                return record

    if desired_site_id is None and cluster_id is None:
        return candidates[0]

    # Operator opt-in (issue #561): reuse a same-name device whose site/cluster
    # differ from the target only when it carries the ``proxbox`` tag.
    return _first_proxbox_tagged(candidates)


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
    device_targets: list[tuple[str, int | None, int | None]],
) -> dict[tuple[str, int | None, int | None], int]:
    """Return existing site pins for devices already compatible with a target."""
    pins: dict[tuple[str, int | None, int | None], int] = {}
    existing_by_name: dict[str, list[object]] = {}
    for device_name, desired_site_id, cluster_id in device_targets:
        if device_name not in existing_by_name:
            try:
                existing_by_name[device_name] = await rest_list_async(
                    nb,
                    "/api/dcim/devices/",
                    query={"name": device_name, "limit": 10},
                )
            except Exception:
                existing_by_name[device_name] = []
        record = _select_existing_device_for_target(
            existing_by_name[device_name],
            desired_site_id=desired_site_id,
            cluster_id=cluster_id,
        )
        if record is None:
            continue
        site_id = _relation_id_or_none(record.get("site"))
        if site_id is not None:
            pins[(device_name, desired_site_id, cluster_id)] = site_id
    return pins


def _cluster_type_payload(mode: str, tag_refs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": mode.capitalize(),
        "slug": mode,
        "description": f"Proxmox {mode} mode",
        "tags": tag_refs,
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
    }


def _device_role_payload(tag_refs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": "Proxmox Node",
        "slug": "proxmox-node",
        "color": "00bcd4",
        "tags": tag_refs,
    }


def _site_payload(cluster_name: str, tag_refs: list[dict[str, object]]) -> dict[str, object]:
    site_slug = f"proxmox-default-site-{_slugify(cluster_name)}"
    return {
        "name": f"Proxmox Default Site - {cluster_name}",
        "slug": site_slug,
        "status": "active",
        "tags": tag_refs,
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
    fields: set[str] = {"name", "cluster"}
    if overwrite_flags is None or overwrite_flags.overwrite_device_status:
        fields.add("status")
    if overwrite_flags is None or overwrite_flags.overwrite_device_description:
        fields.add("description")
    if overwrite_device_role:
        fields.add("role")
    if overwrite_device_type:
        fields.add("device_type")
    if overwrite_device_tags:
        fields.add("tags")
    return fields


@dataclass(slots=True)
class _DeviceTarget:
    cluster_name: str
    node_name: str
    desired_name: str
    effective_name: str
    desired_site_id: int | None
    site_id: int | None
    cluster_id: int | None
    endpoint_id: int | None = None
    existing: NetBoxRecord | None = None
    preserve_manual_name: bool = False
    name_conflict: bool = False


def _collect_cluster_metadata(
    clusters_status: list[object],
) -> tuple[
    dict[str, str],
    dict[str, dict[str, object | None]],
    list[str],
]:
    cluster_modes: dict[str, str] = {}
    cluster_placements: dict[str, dict[str, object | None]] = {}
    node_names: list[str] = []
    for cluster_status in clusters_status:
        cluster_name = str(getattr(cluster_status, "name", "") or "").strip()
        cluster_mode = str(getattr(cluster_status, "mode", "") or "").strip().lower()
        if cluster_name:
            cluster_modes[cluster_name] = cluster_mode or "cluster"
            cluster_placements[cluster_name] = placement_from_source(cluster_status)
        node_names.extend(
            node_name
            for node in (getattr(cluster_status, "node_list", None) or [])
            if (node_name := str(getattr(node, "name", "") or "").strip())
        )
    return cluster_modes, cluster_placements, node_names


async def _reconcile_base_device_dependencies(
    nb: object,
    *,
    cluster_modes: dict[str, str],
    cluster_placements: dict[str, dict[str, object | None]],
    tag_refs: list[dict[str, object]],
) -> dict[str, object]:
    default_site_cluster_names = [
        cluster_name
        for cluster_name in sorted(cluster_modes)
        if not _has_configured_relation(cluster_placements.get(cluster_name, {}), "site")
    ]
    return await rest_bulk_reconcile_phases_async(
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
                },
            ),
        ],
    )


async def _resolve_cluster_placement_records(
    nb: object,
    *,
    cluster_modes: dict[str, str],
    cluster_placements: dict[str, dict[str, object | None]],
    phase_results: dict[str, object],
    tag_refs: list[dict[str, object]],
) -> tuple[dict[str, NetBoxRecord], dict[str, NetBoxRecord]]:
    site_records = phase_results["sites"].records  # type: ignore[union-attr]
    site_by_slug = {str(record.get("slug")): record for record in site_records}
    sites: dict[str, NetBoxRecord] = {}
    tenants: dict[str, NetBoxRecord] = {}
    for cluster_name in sorted(cluster_modes):
        placement = cluster_placements.get(cluster_name, {})
        site_slug = f"proxmox-default-site-{_slugify(cluster_name)}"
        site_record = site_by_slug.get(site_slug) or await _ensure_site(
            nb,
            cluster_name=cluster_name,
            tag_refs=tag_refs,
            placement=placement,
        )
        sites[cluster_name] = site_record
        tenant_record = await _resolve_tenant(nb, placement=placement)
        if tenant_record is not None:
            tenants[cluster_name] = tenant_record
    return sites, tenants


def _cluster_patchable_fields(overwrite_flags: SyncOverwriteFlags | None) -> set[str]:
    fields = {"name", "type", "scope_type", "scope_id", "tenant"}
    if overwrite_flags is None or overwrite_flags.overwrite_cluster_description:
        fields.add("description")
    if overwrite_flags is None or overwrite_flags.overwrite_cluster_tags:
        fields.add("tags")
    return fields


async def _reconcile_cluster_dependencies(
    nb: object,
    *,
    cluster_modes: dict[str, str],
    cluster_type_by_slug: dict[str, NetBoxRecord],
    sites: dict[str, NetBoxRecord],
    tenants: dict[str, NetBoxRecord],
    manufacturer: NetBoxRecord | None,
    tag_refs: list[dict[str, object]],
    overwrite_flags: SyncOverwriteFlags | None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    cluster_payloads = [
        _cluster_payload(
            cluster_name,
            cluster_type_id=_relation_id_or_none(
                cluster_type_by_slug.get(cluster_modes[cluster_name], {}).get("id")
            ),
            mode=cluster_modes[cluster_name],
            tag_refs=tag_refs,
            site_id=_relation_id_or_none(getattr(sites.get(cluster_name), "id", None)),
            tenant_id=_relation_id_or_none(getattr(tenants.get(cluster_name), "id", None)),
        )
        for cluster_name in sorted(cluster_modes)
    ]
    results = await rest_bulk_reconcile_phases_async(
        nb,
        [
            BulkReconcilePhase(
                name="clusters",
                path="/api/virtualization/clusters/",
                payloads=cluster_payloads,
                lookup_fields=["name"],
                schema=NetBoxClusterSyncState,
                patchable_fields=frozenset(_cluster_patchable_fields(overwrite_flags)),
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "type": _relation_id_or_none(record.get("type")),
                    "tenant": _relation_id_or_none(record.get("tenant")),
                    "scope_type": record.get("scope_type"),
                    "scope_id": _relation_id_or_none(record.get("scope_id") or record.get("scope")),
                    "description": record.get("description"),
                    "tags": record.get("tags"),
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
                },
            ),
        ],
    )
    return results, cluster_payloads


async def _write_cluster_sidecars(
    nb: object,
    *,
    cluster_by_name: dict[str, NetBoxRecord],
    cluster_payloads: list[dict[str, object]],
    overwrite_flags: SyncOverwriteFlags | None,
) -> None:
    payload_by_name = {str(payload.get("name")): payload for payload in cluster_payloads}
    for cluster_name in sorted(cluster_by_name):
        payload = payload_by_name.get(cluster_name, {})
        await write_cluster_sync_state(
            nb,
            cluster_id=cluster_by_name[cluster_name].get("id"),
            proxmox_last_updated=_payload_last_updated(payload),
            proxmox_cluster_name=cluster_name,
            overwrite_custom_fields=(
                overwrite_flags is None or overwrite_flags.overwrite_cluster_custom_fields
            ),
        )
        await sync_proxmox_cluster_netbox_link(nb, cluster_name=cluster_name)


def _build_device_targets(
    clusters_status: list[object],
    *,
    cluster_by_name: dict[str, NetBoxRecord],
    sites: dict[str, NetBoxRecord],
) -> list[_DeviceTarget]:
    targets: list[_DeviceTarget] = []
    for cluster_status in clusters_status:
        cluster_name = str(getattr(cluster_status, "name", "") or "").strip()
        cluster_record = cluster_by_name.get(cluster_name)
        desired_site_id = _effective_cluster_site_id(
            cluster_record,
            fallback_site_id=getattr(sites.get(cluster_name), "id", None),
        )
        cluster_id = _relation_id_or_none(getattr(cluster_record, "id", None))
        template = str(getattr(cluster_status, "node_device_name_template", "{node}") or "{node}")
        endpoint = str(getattr(cluster_status, "endpoint_name", "") or cluster_name)
        endpoint_id = _relation_id_or_none(getattr(cluster_status, "db_endpoint_id", None))
        for node in getattr(cluster_status, "node_list", None) or []:
            node_name = str(getattr(node, "name", "") or "").strip()
            if node_name:
                try:
                    desired_name = render_node_device_name(
                        node_name, cluster_name, endpoint, template
                    )
                except NodeDeviceNameError:
                    continue
                targets.append(
                    _DeviceTarget(
                        cluster_name=cluster_name,
                        node_name=node_name,
                        desired_name=desired_name,
                        effective_name=desired_name,
                        desired_site_id=desired_site_id,
                        site_id=desired_site_id,
                        cluster_id=cluster_id,
                        endpoint_id=endpoint_id,
                    )
                )
    return targets


async def _device_from_identity_sidecar(
    nb: object,
    target: _DeviceTarget,
) -> NetBoxRecord | None:
    if not target.cluster_name:
        return None
    query: dict[str, object] = {
        "proxmox_cluster_name": target.cluster_name,
        "proxmox_node_name": target.node_name,
        "limit": 3,
    }
    if target.endpoint_id is not None:
        query["proxmox_endpoint_raw_id"] = target.endpoint_id
    try:
        sidecars = await rest_list_async(
            nb,
            "/api/plugins/proxbox/sync-state/devices/",
            query=query,
        )
    except Exception:
        return None
    matching_sidecars = sidecars
    if target.endpoint_id is not None:
        matching_sidecars = [
            sidecar
            for sidecar in sidecars
            if (
                _relation_id_or_none(sidecar.get("proxmox_endpoint_raw_id")) is None
                or _relation_id_or_none(sidecar.get("proxmox_endpoint_raw_id"))
                == target.endpoint_id
            )
        ]
    else:
        claimed_endpoint_ids = {
            endpoint_id
            for sidecar in sidecars
            if (endpoint_id := _relation_id_or_none(sidecar.get("proxmox_endpoint_raw_id")))
            is not None
        }
        if len(claimed_endpoint_ids) > 1:
            raise ProxboxException(
                message="Ambiguous Proxmox node device identity",
                detail=(
                    f"Multiple Proxmox endpoints claim node {target.node_name!r} in "
                    f"cluster {target.cluster_name!r}."
                ),
            )
    device_ids = {
        device_id
        for sidecar in matching_sidecars
        if (device_id := _relation_id_or_none(sidecar.get("device"))) is not None
    }
    if len(device_ids) > 1:
        return await _disambiguate_sidecar_devices(nb, target, sorted(device_ids))
    if not device_ids:
        return None
    return await rest_first_async(
        nb,
        "/api/dcim/devices/",
        query={"id": next(iter(device_ids)), "limit": 2},
    )


async def _disambiguate_sidecar_devices(
    nb: object,
    target: _DeviceTarget,
    device_ids: list[int],
) -> NetBoxRecord | None:
    """Resolve several identity sidecars that point at different NetBox devices.

    Sidecars for deleted devices are stale and ignored. Device names are not
    evidence: operators may rename a node device. Among the remaining devices the
    target cluster decides ownership. A site match counts only for devices that
    have no cluster, because a site can hold several clusters. Anything still
    ambiguous fails closed so the wrong device is never adopted.
    """
    live: list[NetBoxRecord] = []
    for device_id in device_ids:
        record = await rest_first_async(
            nb,
            "/api/dcim/devices/",
            query={"id": device_id, "limit": 2},
        )
        if record is not None:
            live.append(record)
    if len(live) <= 1:
        return live[0] if live else None
    placed = [
        record
        for record in live
        if target.cluster_id is not None
        and _relation_id_or_none(record.get("cluster")) == target.cluster_id
    ]
    if not placed:
        placed = [
            record
            for record in live
            if target.desired_site_id is not None
            and _relation_id_or_none(record.get("cluster")) is None
            and _relation_id_or_none(record.get("site")) == target.desired_site_id
        ]
    if len(placed) != 1:
        raise ProxboxException(
            message="Ambiguous Proxmox node device identity",
            detail=(
                f"Multiple NetBox devices claim node {target.node_name!r} in "
                f"cluster {target.cluster_name!r}."
            ),
        )
    logger.warning(
        "Ignored conflicting identity sidecars for node %r in cluster %r (device ids %s)",
        target.node_name,
        target.cluster_name,
        device_ids,
    )
    return placed[0]


async def _resolve_existing_device_for_target(
    nb: object,
    target: _DeviceTarget,
) -> NetBoxRecord | None:
    sidecar_match = await _device_from_identity_sidecar(nb, target)
    if sidecar_match is not None:
        return sidecar_match
    legacy_records = await rest_list_async(
        nb,
        "/api/dcim/devices/",
        query={"name": target.node_name, "limit": 10},
    )
    legacy_match = _select_existing_device_for_target(
        legacy_records,
        desired_site_id=target.desired_site_id,
        cluster_id=target.cluster_id,
    )
    if legacy_match is not None or target.desired_name == target.node_name:
        return legacy_match
    desired_records = await rest_list_async(
        nb,
        "/api/dcim/devices/",
        query={"name": target.desired_name, "limit": 10},
    )
    return _select_existing_device_for_target(
        desired_records,
        desired_site_id=target.desired_site_id,
        cluster_id=target.cluster_id,
    )


def _device_name_is_proxbox_managed(record: NetBoxRecord, node_name: str) -> bool:
    current_name = str(record.get("name") or "")
    if current_name == node_name:
        return True
    return (
        _record_has_tag(record, PROXBOX_TAG)
        and str(record.get("description") or "") == f"Proxmox Node {current_name}"
    )


def _mark_duplicate_target_destinations(targets: list[_DeviceTarget]) -> None:
    targets_by_destination: dict[tuple[str, int | None], list[_DeviceTarget]] = {}
    for target in targets:
        if not target.preserve_manual_name:
            targets_by_destination.setdefault((target.desired_name, target.site_id), []).append(
                target
            )
    for (desired_name, site_id), destination_targets in targets_by_destination.items():
        identities = {
            (target.endpoint_id, target.cluster_name, target.node_name)
            for target in destination_targets
        }
        if len(identities) < 2:
            continue
        for target in destination_targets:
            target.name_conflict = True
        logger.error(
            "Skipping %d Proxmox nodes: rendered device name %r is duplicated "
            "within site id=%r by identities=%r",
            len(destination_targets),
            desired_name,
            site_id,
            sorted(identities, key=repr),
        )


async def _persisted_name_conflict(nb: object, target: _DeviceTarget) -> NetBoxRecord | None:
    occupants = await rest_list_async(
        nb,
        "/api/dcim/devices/",
        query={"name": target.desired_name, "site_id": target.site_id, "limit": 2},
    )
    existing_id = (
        _relation_id_or_none(target.existing.get("id")) if target.existing is not None else None
    )
    return next(
        (
            occupant
            for occupant in occupants
            if str(occupant.get("name") or "") == target.desired_name
            and _relation_id_or_none(occupant.get("site")) == target.site_id
            and _relation_id_or_none(occupant.get("id")) != existing_id
        ),
        None,
    )


async def _prepare_device_targets(
    nb: object,
    targets: list[_DeviceTarget],
) -> None:
    pending_renames: list[tuple[NetBoxRecord, str]] = []
    for target in targets:
        existing = await _resolve_existing_device_for_target(nb, target)
        target.existing = existing
        target.site_id = _existing_device_site_pin(existing, target.desired_site_id)
        if existing is None:
            continue
        current_name = str(existing.get("name") or "")
        if current_name == target.desired_name:
            continue
        if not _device_name_is_proxbox_managed(existing, target.node_name):
            target.effective_name = current_name
            target.preserve_manual_name = True
            continue
        pending_renames.append((existing, target.desired_name))

    _mark_duplicate_target_destinations(targets)

    for target in targets:
        if target.preserve_manual_name or target.name_conflict:
            continue
        conflict = await _persisted_name_conflict(nb, target)
        if conflict is None:
            continue
        target.name_conflict = True
        logger.error(
            "Skipping Proxmox node %r in cluster %r: rendered device name %r "
            "is already used by NetBox device id=%r in site id=%r",
            target.node_name,
            target.cluster_name,
            target.desired_name,
            conflict.get("id"),
            target.site_id,
        )

    conflicting_ids = {
        id(target.existing) for target in targets if target.name_conflict and target.existing
    }
    for existing, desired_name in pending_renames:
        if id(existing) in conflicting_ids:
            continue
        setattr(existing, "name", desired_name)
        await existing.save()


def _target_device_payload(
    target: _DeviceTarget,
    *,
    device_type_id: int | None,
    role_id: int | None,
    tag_refs: list[dict[str, object]],
) -> dict[str, object]:
    payload = _device_payload(
        target.effective_name,
        cluster_id=target.cluster_id,
        device_type_id=device_type_id,
        role_id=role_id,
        site_id=target.site_id,
        tag_refs=tag_refs,
    )
    if target.preserve_manual_name and target.existing is not None:
        payload["description"] = target.existing.get("description") or (
            f"Proxmox node {target.node_name} (operator-managed name)"
        )
    return payload


async def _reconcile_device_targets(
    nb: object,
    *,
    targets: list[_DeviceTarget],
    device_type_id: int | None,
    role_id: int | None,
    tag_refs: list[dict[str, object]],
    patchable_fields: set[str],
    overwrite_flags: SyncOverwriteFlags | None,
) -> dict[tuple[int | None, str, str], NetBoxRecord]:
    payloads = [
        _target_device_payload(
            target,
            device_type_id=device_type_id,
            role_id=role_id,
            tag_refs=tag_refs,
        )
        for target in targets
        if not target.name_conflict
    ]
    active_targets = [target for target in targets if not target.name_conflict]
    results = await rest_bulk_reconcile_phases_async(
        nb,
        [
            BulkReconcilePhase(
                name="devices",
                path="/api/dcim/devices/",
                payloads=payloads,
                lookup_fields=["name", "site"],
                lookup_query_field_map={"site": "site_id"},
                schema=NetBoxDeviceSyncState,
                patchable_fields=frozenset(patchable_fields),
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "status": record.get("status"),
                    "cluster": _relation_id_or_none(record.get("cluster")),
                    "device_type": _relation_id_or_none(record.get("device_type")),
                    "role": _relation_id_or_none(record.get("role")),
                    "site": _relation_id_or_none(record.get("site")),
                    "description": record.get("description"),
                    "tags": record.get("tags"),
                },
                selector=_device_selector,
            )
        ],
    )
    records_by_lookup = {
        (str(record.get("name")), _relation_id_or_none(record.get("site"))): record
        for record in results["devices"].records
    }
    devices: dict[tuple[int | None, str, str], NetBoxRecord] = {}
    for target, payload in zip(active_targets, payloads, strict=True):
        record = records_by_lookup.get((target.effective_name, target.site_id))
        if record is None:
            continue
        devices[(target.endpoint_id, target.cluster_name, target.node_name)] = record
        await write_device_sync_state(
            nb,
            device_id=record.get("id"),
            proxmox_last_updated=_payload_last_updated(payload),
            proxmox_node_name=target.node_name,
            proxmox_cluster_name=target.cluster_name,
            proxmox_endpoint_raw_id=target.endpoint_id,
            overwrite_custom_fields=(
                overwrite_flags is None or overwrite_flags.overwrite_device_custom_fields
            ),
        )
    return devices


async def ensure_proxmox_devices_bulk(
    nb: object,
    *,
    clusters_status: list[object] | None,
    tag_refs: list[dict[str, object]],
    overwrite_device_role: bool = True,
    overwrite_device_type: bool = True,
    overwrite_device_tags: bool = True,
    overwrite_flags: SyncOverwriteFlags | None = None,
) -> dict[tuple[int | None, str, str], NetBoxRecord]:
    """Create/update Proxmox prerequisite NetBox objects in dependency order."""
    if not clusters_status:
        return {}

    cluster_modes, cluster_placements, node_names = _collect_cluster_metadata(clusters_status)
    if not cluster_modes and not node_names:
        return {}

    base_results = await _reconcile_base_device_dependencies(
        nb,
        cluster_modes=cluster_modes,
        cluster_placements=cluster_placements,
        tag_refs=tag_refs,
    )
    cluster_type_by_slug = {
        str(record.get("slug")): record for record in base_results["cluster_types"].records
    }
    manufacturer_records = base_results["manufacturers"].records
    role_records = base_results["device_roles"].records
    manufacturer = manufacturer_records[0] if manufacturer_records else None
    role = role_records[0] if role_records else None
    sites, tenants = await _resolve_cluster_placement_records(
        nb,
        cluster_modes=cluster_modes,
        cluster_placements=cluster_placements,
        phase_results=base_results,
        tag_refs=tag_refs,
    )
    dependency_results, cluster_payloads = await _reconcile_cluster_dependencies(
        nb,
        cluster_modes=cluster_modes,
        cluster_type_by_slug=cluster_type_by_slug,
        sites=sites,
        tenants=tenants,
        manufacturer=manufacturer,
        tag_refs=tag_refs,
        overwrite_flags=overwrite_flags,
    )
    cluster_by_name = {
        str(record.get("name")): record for record in dependency_results["clusters"].records
    }
    await _write_cluster_sidecars(
        nb,
        cluster_by_name=cluster_by_name,
        cluster_payloads=cluster_payloads,
        overwrite_flags=overwrite_flags,
    )
    device_type_records = dependency_results["device_types"].records
    device_type = device_type_records[0] if device_type_records else None
    targets = _build_device_targets(
        clusters_status,
        cluster_by_name=cluster_by_name,
        sites=sites,
    )
    await _prepare_device_targets(nb, targets)
    patchable_fields = _compute_device_patchable_fields(
        overwrite_flags,
        overwrite_device_role,
        overwrite_device_type,
        overwrite_device_tags,
    )
    return await _reconcile_device_targets(
        nb,
        targets=targets,
        device_type_id=_relation_id_or_none(getattr(device_type, "id", None)),
        role_id=_relation_id_or_none(getattr(role, "id", None)),
        tag_refs=tag_refs,
        patchable_fields=patchable_fields,
        overwrite_flags=overwrite_flags,
    )


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
    nb: object,
    *,
    cluster_name: str,
    cluster_type_id: int | None,
    mode: str,
    tag_refs: list[dict[str, object]],
    site_id: int | None = None,
    tenant_id: int | None = None,
    overwrite_flags: SyncOverwriteFlags | None = None,
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
        effective_tag_refs: list[dict[str, object]] = list(tag_refs)
        # Soft contract (issue #362): only stamp the discovery slug when the tag
        # exists in NetBox, so a missing tag never fails the cluster create.
        if await resolve_discovery_tag_id(nb, DISCOVERY_TAG_CLUSTER) is not None:
            effective_tag_refs.append(discovery_tag_ref(DISCOVERY_TAG_CLUSTER))
    else:
        existing_tags = (
            existing_cluster.serialize().get("tags")
            if hasattr(existing_cluster, "serialize")
            else None
        )
        effective_tag_refs = merge_tag_refs(list(tag_refs), existing_tags)

    payload = _cluster_payload(
        cluster_name,
        cluster_type_id=cluster_type_id,
        mode=mode,
        tag_refs=effective_tag_refs,
        site_id=site_id,
        tenant_id=tenant_id,
    )
    cluster = await rest_reconcile_async(
        nb,
        "/api/virtualization/clusters/",
        lookup={"name": cluster_name},
        payload=payload,
        schema=NetBoxClusterSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "type": _relation_id_or_none(record.get("type")),
            "tenant": _relation_id_or_none(record.get("tenant")),
            "scope_type": record.get("scope_type"),
            "scope_id": _relation_id_or_none(record.get("scope_id") or record.get("scope")),
            "description": record.get("description"),
            "tags": record.get("tags"),
        },
    )
    await write_cluster_sync_state(
        nb,
        cluster_id=cluster.get("id") if isinstance(cluster, dict) else getattr(cluster, "id", None),
        proxmox_last_updated=_payload_last_updated(payload),
        proxmox_cluster_name=cluster_name,
        overwrite_custom_fields=(
            overwrite_flags is None or overwrite_flags.overwrite_cluster_custom_fields
        ),
    )
    return cluster


async def _ensure_manufacturer(nb: object, *, tag_refs: list[dict[str, object]]) -> NetBoxRecord:
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
        },
        schema=NetBoxDeviceTypeSyncState,
        current_normalizer=lambda record: {
            "model": record.get("model"),
            "slug": record.get("slug"),
            "manufacturer": record.get("manufacturer"),
            "tags": record.get("tags"),
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
        },
        schema=NetBoxDeviceRoleSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "color": record.get("color"),
            "tags": record.get("tags"),
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
        },
        schema=NetBoxSiteSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "status": record.get("status"),
            "tags": record.get("tags"),
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
    cluster_name: str = "",
    endpoint_name: str = "",
    node_device_name_template: str = "{node}",
    endpoint_id: int | None = None,
    overwrite_device_role: bool = True,
    overwrite_device_type: bool = True,
    overwrite_device_tags: bool = True,
    overwrite_flags: SyncOverwriteFlags | None = None,
) -> NetBoxRecord:
    desired_name = render_node_device_name(
        device_name,
        cluster_name,
        endpoint_name or cluster_name,
        node_device_name_template,
    )
    target = _DeviceTarget(
        cluster_name=cluster_name,
        node_name=device_name,
        desired_name=desired_name,
        effective_name=desired_name,
        desired_site_id=site_id,
        site_id=site_id,
        cluster_id=cluster_id,
        endpoint_id=endpoint_id,
    )
    await _prepare_device_targets(nb, [target])
    existing_device = target.existing
    # Save the cluster-authoritative site BEFORE the pin.  When the cluster is
    # being reassigned, the device's site must be updated to match the cluster's
    # scope_site — NetBox enforces device.site == cluster.scope_site on every
    # write.  Keeping the pinned (old) site in the payload while changing the
    # cluster produces "The assigned cluster belongs to a different site".
    _desired_site_id = target.desired_site_id
    site_id = target.site_id

    # First-discovery audit tag (issue #362). Stamp the node-discovery slug
    # in the create payload only; on update, merge the desired tag refs with
    # whatever the existing device already carries so neither the discovery
    # tag nor operator-added tags get stripped.
    if existing_device is None:
        effective_tag_refs: list[dict[str, object]] = list(tag_refs)
        # The first-discovery audit tag is a soft contract (issue #362): stamp
        # it only when it actually exists in NetBox. Bootstrap normally creates
        # the four discovery slugs, but when it has not (e.g. bootstrap disabled
        # or not yet run), sending the slug-only ref makes NetBox reject the
        # device create with "Related object not found". A missing discovery tag
        # must never block the sync, so skip stamping instead of failing.
        if await resolve_discovery_tag_id(nb, DISCOVERY_TAG_NODE) is not None:
            effective_tag_refs.append(discovery_tag_ref(DISCOVERY_TAG_NODE))
    else:
        effective_tag_refs = merge_tag_refs(
            list(tag_refs),
            existing_device.get("tags"),
        )

    payload = {
        "name": target.effective_name,
        "tags": effective_tag_refs,
        "cluster": cluster_id,
        "status": "active",
        "description": f"Proxmox Node {target.effective_name}",
        "device_type": device_type_id,
        "role": role_id,
        "site": site_id,
    }

    allowed = _compute_device_patchable_fields(
        overwrite_flags,
        overwrite_device_role,
        overwrite_device_type,
        overwrite_device_tags,
    )
    if target.preserve_manual_name:
        allowed.discard("name")
        payload["description"] = existing_device.get("description") or (
            f"Proxmox node {device_name} (operator-managed name)"
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
            }
        )
        current_payload = current_model.model_dump(exclude_none=True, by_alias=True)

        patch_payload = {
            key: value
            for key, value in desired_payload.items()
            if current_payload.get(key) != value and key in allowed
        }
        # NetBox requires device.site == cluster.scope_site.  When the cluster
        # assignment is changing, inject the corresponding site into the patch so
        # the single write satisfies both constraints.  The site is intentionally
        # not in `allowed` for the general case (preserves user-assigned sites when
        # the cluster is not moving), so it must be injected here explicitly.
        if "cluster" in patch_payload and _desired_site_id is not None:
            patch_payload["site"] = _desired_site_id
        if patch_payload:
            for field, value in patch_payload.items():
                setattr(existing_device, field, value)
            await existing_device.save()
        await write_device_sync_state(
            nb,
            device_id=(
                existing_device.get("id")
                if isinstance(existing_device, dict)
                else getattr(existing_device, "id", None)
            ),
            proxmox_last_updated=_payload_last_updated(payload),
            proxmox_node_name=device_name,
            proxmox_cluster_name=cluster_name,
            proxmox_endpoint_raw_id=endpoint_id,
            overwrite_custom_fields=(
                overwrite_flags is None or overwrite_flags.overwrite_device_custom_fields
            ),
        )
        return existing_device

    device = await rest_reconcile_async(
        nb,
        "/api/dcim/devices/",
        lookup={"name": target.effective_name, "site_id": site_id},
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
        },
    )
    await write_device_sync_state(
        nb,
        device_id=device.get("id") if isinstance(device, dict) else getattr(device, "id", None),
        proxmox_last_updated=_payload_last_updated(payload),
        proxmox_node_name=device_name,
        proxmox_cluster_name=cluster_name,
        proxmox_endpoint_raw_id=endpoint_id,
        overwrite_custom_fields=(
            overwrite_flags is None or overwrite_flags.overwrite_device_custom_fields
        ),
    )
    return device


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
