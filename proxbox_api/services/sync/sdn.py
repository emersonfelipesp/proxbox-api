"""Read-only Proxmox SDN inventory and NetBox reconciliation."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from proxmox_sdk.sdk.exceptions import ResourceException
from pydantic import BaseModel, ConfigDict, Field

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_first_async, rest_reconcile_async_with_status
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services.sync.backup_routines import _get_netbox_endpoint_id

if TYPE_CHECKING:
    from proxbox_api.utils.streaming import WebSocketSSEBridge


_UNSUPPORTED_STATUS_CODES = {404, 501}
_SLUG_FRAGMENT_RE = re.compile(r"[^a-z0-9]+")
_BGP_COMMUNITY_RE = re.compile(r"^(?P<left>\d{1,10}):(?P<right>\d{1,10})$")
_MANAGED_L2VPN_TYPES = {"evpn": "vxlan-evpn", "vxlan": "vxlan"}
_TERMINATION_TARGET_TYPES = {"virtualization.vminterface", "dcim.interface", "ipam.vlan"}
_VALID_SYNC_MODES = frozenset({"always", "bootstrap_only", "disabled"})
_BGP_API_PEER_GROUPS = "/api/plugins/bgp/peer-group/"
_BGP_API_SESSIONS = "/api/plugins/bgp/session/"
_BGP_API_ROUTING_POLICIES = "/api/plugins/bgp/routing-policy/"
_BGP_API_ROUTING_POLICY_RULES = "/api/plugins/bgp/routing-policy-rule/"
_BGP_API_PREFIX_LISTS = "/api/plugins/bgp/prefix-list/"
_BGP_API_PREFIX_LIST_RULES = "/api/plugins/bgp/prefix-list-rule/"
_BGP_API_COMMUNITIES = "/api/plugins/bgp/community/"
_NETBOX_BGP_TARGET_TYPES = {
    "community": "netbox_bgp.community",
    "peer_group": "netbox_bgp.bgppeergroup",
    "prefix_list": "netbox_bgp.prefixlist",
    "prefix_list_rule": "netbox_bgp.prefixlistrule",
    "routing_policy": "netbox_bgp.routingpolicy",
    "routing_policy_rule": "netbox_bgp.routingpolicyrule",
    "session": "netbox_bgp.bgpsession",
}
_RELATION_ID_FIELDS = {
    "endpoint",
    "export_policies",
    "import_policies",
    "import_targets",
    "export_targets",
    "l2vpn",
    "local_address",
    "remote_address",
    "local_as",
    "remote_as",
    "match_community",
    "match_community_list",
    "match_aspath_list",
    "match_ip_address",
    "match_ipv6_address",
    "peer_group",
    "prefix",
    "prefix_list",
    "prefix_list_in",
    "prefix_list_out",
    "routing_policy",
}


class _SdnSchema(BaseModel):
    """Base schema for lenient SDN API payloads."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class SdnFabricSchema(_SdnSchema):
    """A single SDN fabric object."""

    endpoint_id: int | None = None
    cluster_name: str | None = None
    fabric: str | None = None
    type: str | None = None
    advertise_subnets: bool | None = None
    disable_arp_nd_suppression: bool | None = None
    vrf_vxlan: int | None = None
    peers: str | None = None
    asn: int | None = None
    status: str = "ok"
    error: str | None = None
    raw_config: dict[str, object] = Field(default_factory=dict)


class SdnRouteMapSchema(_SdnSchema):
    """A single SDN route-map entry."""

    endpoint_id: int | None = None
    cluster_name: str | None = None
    name: str | None = None
    action: str | None = None
    match_peer: str | None = None
    match_ip: str | None = None
    set_community: str | None = None
    order: int | None = None
    status: str = "ok"
    error: str | None = None
    raw_config: dict[str, object] = Field(default_factory=dict)


class SdnPrefixListSchema(_SdnSchema):
    """A single SDN prefix-list entry."""

    endpoint_id: int | None = None
    cluster_name: str | None = None
    name: str | None = None
    cidr: str | None = None
    action: str | None = None
    le: int | None = None
    ge: int | None = None
    status: str = "ok"
    error: str | None = None
    raw_config: dict[str, object] = Field(default_factory=dict)


class SdnControllerSchema(_SdnSchema):
    """A single Proxmox SDN controller object."""

    endpoint_id: int | None = None
    cluster_name: str | None = None
    controller: str | None = None
    type: str | None = None
    asn: int | None = None
    peers: str | None = None
    node: str | None = None
    loopback: str | None = None
    state: str | None = None
    status: str = "ok"
    error: str | None = None
    raw_config: dict[str, object] = Field(default_factory=dict)


class SdnZoneSchema(_SdnSchema):
    """A single Proxmox SDN zone object."""

    endpoint_id: int | None = None
    cluster_name: str | None = None
    zone: str | None = None
    type: str | None = None
    controller: str | None = None
    vrf_vxlan: int | None = None
    tag: int | None = None
    mtu: int | None = None
    dns: str | None = None
    ipam: str | None = None
    rt_import: str | None = None
    state: str | None = None
    pending: dict[str, object] | None = None
    status: str = "ok"
    error: str | None = None
    raw_config: dict[str, object] = Field(default_factory=dict)


class SdnVNetSchema(_SdnSchema):
    """A single Proxmox SDN VNet object."""

    endpoint_id: int | None = None
    cluster_name: str | None = None
    vnet: str | None = None
    zone: str | None = None
    type: str | None = None
    tag: int | None = None
    alias: str | None = None
    vlanaware: bool | None = None
    state: str | None = None
    pending: dict[str, object] | None = None
    status: str = "ok"
    error: str | None = None
    raw_config: dict[str, object] = Field(default_factory=dict)


class SdnSubnetSchema(_SdnSchema):
    """A single Proxmox SDN VNet subnet object."""

    endpoint_id: int | None = None
    cluster_name: str | None = None
    vnet: str | None = None
    zone: str | None = None
    subnet: str | None = None
    type: str | None = None
    gateway: str | None = None
    snat: bool | None = None
    status: str = "ok"
    error: str | None = None
    raw_config: dict[str, object] = Field(default_factory=dict)


class SdnNodeStatusSchema(_SdnSchema):
    """Node-local SDN status, bridge, MAC-VRF, or IP-VRF row."""

    endpoint_id: int | None = None
    cluster_name: str | None = None
    node: str | None = None
    zone: str | None = None
    vnet: str | None = None
    kind: str
    name: str | None = None
    status: str = "ok"
    error: str | None = None
    raw_config: dict[str, object] = Field(default_factory=dict)


@dataclass(slots=True)
class SdnInventory:
    """Collected Proxmox SDN inventory for one endpoint."""

    endpoint_id: int | None
    endpoint_name: str
    cluster_name: str
    controllers: list[SdnControllerSchema] = field(default_factory=list)
    zones: list[SdnZoneSchema] = field(default_factory=list)
    vnets: list[SdnVNetSchema] = field(default_factory=list)
    subnets: list[SdnSubnetSchema] = field(default_factory=list)
    fabrics: list[SdnFabricSchema] = field(default_factory=list)
    route_maps: list[SdnRouteMapSchema] = field(default_factory=list)
    prefix_lists: list[SdnPrefixListSchema] = field(default_factory=list)
    node_status: list[SdnNodeStatusSchema] = field(default_factory=list)
    skipped_warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SdnSyncCounters:
    """Counters emitted by the SDN SSE stage."""

    controllers: int = 0
    zones: int = 0
    vnets: int = 0
    subnets: int = 0
    l2vpns: int = 0
    route_targets: int = 0
    terminations: int = 0
    skipped: int = 0
    stale: int = 0
    plugin_metadata: int = 0
    bgp_peer_groups: int = 0
    bgp_sessions: int = 0
    bgp_routing_policies: int = 0
    bgp_routing_policy_rules: int = 0
    bgp_prefix_lists: int = 0
    bgp_prefix_list_rules: int = 0
    bgp_communities: int = 0
    per_endpoint_errors: dict[str, int] = field(default_factory=dict)
    object_errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "controllers": self.controllers,
            "zones": self.zones,
            "vnets": self.vnets,
            "subnets": self.subnets,
            "l2vpns": self.l2vpns,
            "route_targets": self.route_targets,
            "terminations": self.terminations,
            "skipped": self.skipped,
            "stale": self.stale,
            "plugin_metadata": self.plugin_metadata,
            "bgp_peer_groups": self.bgp_peer_groups,
            "bgp_sessions": self.bgp_sessions,
            "bgp_routing_policies": self.bgp_routing_policies,
            "bgp_routing_policy_rules": self.bgp_routing_policy_rules,
            "bgp_prefix_lists": self.bgp_prefix_lists,
            "bgp_prefix_list_rules": self.bgp_prefix_list_rules,
            "bgp_communities": self.bgp_communities,
            "per_endpoint_errors": self.per_endpoint_errors,
            "object_errors": self.object_errors,
            "warnings": self.warnings,
        }

    def record_endpoint_error(self, endpoint: str) -> None:
        self.per_endpoint_errors[endpoint] = self.per_endpoint_errors.get(endpoint, 0) + 1

    def record_object_error(
        self,
        endpoint: str,
        *,
        kind: str,
        name: str,
        error: Exception,
    ) -> None:
        self.record_endpoint_error(endpoint)
        self.object_errors.append(
            {
                "kind": kind,
                "name": name,
                "error": str(error),
            }
        )

    def record_object_warning(self, *, kind: str, name: str, warning: str) -> None:
        self.warnings.append(
            {
                "kind": kind,
                "name": name,
                "warning": warning,
            }
        )


def _to_mapping(raw: object) -> dict[str, object]:
    if hasattr(raw, "model_dump"):
        return raw.model_dump(mode="python", by_alias=True, exclude_none=True)
    if isinstance(raw, dict):
        return dict(raw)
    if hasattr(raw, "root"):
        nested = getattr(raw, "root")
        return _to_mapping(nested)
    return {}


def _rows(raw: object) -> list[object]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if hasattr(raw, "root"):
        root = getattr(raw, "root")
        return root if isinstance(root, list) else [root]
    return [raw]


def _value(data: dict[str, object], *keys: str) -> object:
    for key in keys:
        if key in data:
            return data[key]
        alt = key.replace("-", "_")
        if alt in data:
            return data[alt]
        alt = key.replace("_", "-")
        if alt in data:
            return data[alt]
    return None


def _text(data: dict[str, object], *keys: str) -> str | None:
    raw = _value(data, *keys)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _int(data: dict[str, object], *keys: str) -> int | None:
    raw = _value(data, *keys)
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _bool(data: dict[str, object], *keys: str) -> bool | None:
    raw = _value(data, *keys)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return bool(raw)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _raw_dict(data: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in data.items() if value is not None}


def _split_csv(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _slug(value: object) -> str:
    text = str(value or "unknown").strip().lower()
    slug = _SLUG_FRAGMENT_RE.sub("-", text).strip("-")
    return slug or "unknown"


def _relation_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        raw = value.get("id")
        if isinstance(raw, int):
            return raw
    return None


def _relation_ids(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    ids: list[int] = []
    for item in value:
        record_id = _relation_id(item)
        if record_id is not None:
            ids.append(record_id)
    return ids


def _content_type_value(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip().lower() or None
    if isinstance(value, dict):
        app_label = value.get("app_label")
        model = value.get("model")
        if app_label and model:
            return f"{app_label}.{model}".lower()
    return None


def _netbox_status(state: str | None, pending: dict[str, object] | None = None) -> str:
    normalized = (state or "").strip().lower()
    if normalized == "deleted":
        return "decommissioning"
    if normalized in {"new", "pending"} or pending:
        return "planned"
    return "active"


def _is_unsupported_sdn_error(error: Exception) -> bool:
    if isinstance(error, ResourceException):
        if error.status_code in _UNSUPPORTED_STATUS_CODES:
            return True
    message = str(error).lower()
    return "no such api path" in message or "not implemented" in message


async def _fetch_rows(px: object, path: str, inventory: SdnInventory) -> list[object]:
    try:
        raw = await resolve_async(px.session(path).get())
    except Exception as error:  # noqa: BLE001
        if _is_unsupported_sdn_error(error):
            inventory.skipped_warnings.append(f"{inventory.cluster_name}: {path} unsupported")
            return []
        inventory.errors.append(f"{inventory.cluster_name}: {path}: {error}")
        logger.warning("Error fetching Proxmox SDN path %s for %s: %s", path, px.name, error)
        return []
    return _rows(raw)


def _node_names(px: object) -> list[str]:
    names: list[str] = []
    for row in getattr(px, "cluster_status", []) or []:
        data = _to_mapping(row)
        if _text(data, "type") != "node":
            continue
        name = _text(data, "name", "node")
        if name and name not in names:
            names.append(name)
    fallback = getattr(px, "node_name", None) or getattr(px, "name", None)
    if not names and fallback:
        names.append(str(fallback))
    return names


def _to_fabric(
    cluster_name: str,
    raw: object,
    endpoint_id: int | None = None,
) -> SdnFabricSchema:
    data = _to_mapping(raw)
    return SdnFabricSchema(
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        fabric=_text(data, "fabric", "id", "name"),
        type=_text(data, "type"),
        advertise_subnets=_bool(data, "advertise-subnets", "advertise_subnets"),
        disable_arp_nd_suppression=_bool(
            data,
            "disable-arp-nd-suppression",
            "disable_arp_nd_suppression",
        ),
        vrf_vxlan=_int(data, "vrf-vxlan", "vrf_vxlan"),
        asn=_int(data, "asn"),
        peers=_text(data, "peers"),
        raw_config=_raw_dict(data),
    )


def _to_route_map(
    cluster_name: str,
    raw: object,
    endpoint_id: int | None = None,
) -> SdnRouteMapSchema:
    data = _to_mapping(raw)
    return SdnRouteMapSchema(
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        name=_text(data, "name", "id"),
        action=_text(data, "action"),
        match_peer=_text(data, "match-peer", "match_peer"),
        match_ip=_text(data, "match-ip", "match_ip"),
        set_community=_text(data, "set-community", "set_community"),
        order=_int(data, "order"),
        raw_config=_raw_dict(data),
    )


def _to_prefix_list(
    cluster_name: str,
    raw: object,
    endpoint_id: int | None = None,
) -> SdnPrefixListSchema:
    data = _to_mapping(raw)
    return SdnPrefixListSchema(
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        name=_text(data, "name", "id"),
        cidr=_text(data, "cidr"),
        action=_text(data, "action"),
        le=_int(data, "le"),
        ge=_int(data, "ge"),
        raw_config=_raw_dict(data),
    )


def _to_controller(
    cluster_name: str,
    raw: object,
    endpoint_id: int | None = None,
) -> SdnControllerSchema:
    data = _to_mapping(raw)
    return SdnControllerSchema(
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        controller=_text(data, "controller", "id", "name"),
        type=_text(data, "type"),
        asn=_int(data, "asn"),
        peers=_text(data, "peers"),
        node=_text(data, "node"),
        loopback=_text(data, "loopback"),
        state=_text(data, "state"),
        raw_config=_raw_dict(data),
    )


def _to_zone(
    cluster_name: str,
    raw: object,
    endpoint_id: int | None = None,
) -> SdnZoneSchema:
    data = _to_mapping(raw)
    pending = _value(data, "pending")
    return SdnZoneSchema(
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        zone=_text(data, "zone", "id", "name"),
        type=_text(data, "type"),
        controller=_text(data, "controller"),
        vrf_vxlan=_int(data, "vrf-vxlan", "vrf_vxlan"),
        tag=_int(data, "tag"),
        mtu=_int(data, "mtu"),
        dns=_text(data, "dns"),
        ipam=_text(data, "ipam"),
        rt_import=_text(data, "rt-import", "rt_import"),
        state=_text(data, "state"),
        pending=pending if isinstance(pending, dict) else None,
        raw_config=_raw_dict(data),
    )


def _to_vnet(
    cluster_name: str,
    raw: object,
    endpoint_id: int | None = None,
) -> SdnVNetSchema:
    data = _to_mapping(raw)
    pending = _value(data, "pending")
    return SdnVNetSchema(
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        vnet=_text(data, "vnet", "id", "name"),
        zone=_text(data, "zone"),
        type=_text(data, "type"),
        tag=_int(data, "tag"),
        alias=_text(data, "alias"),
        vlanaware=_bool(data, "vlanaware", "vlan-aware", "vlan_aware"),
        state=_text(data, "state"),
        pending=pending if isinstance(pending, dict) else None,
        raw_config=_raw_dict(data),
    )


def _to_subnet(
    cluster_name: str,
    vnet_name: str,
    zone_name: str | None,
    raw: object,
    endpoint_id: int | None = None,
) -> SdnSubnetSchema:
    data = _to_mapping(raw)
    return SdnSubnetSchema(
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        vnet=vnet_name,
        zone=zone_name,
        subnet=_text(data, "subnet", "cidr", "id"),
        type=_text(data, "type"),
        gateway=_text(data, "gateway"),
        snat=_bool(data, "snat"),
        raw_config=_raw_dict(data),
    )


def _to_node_status(
    *,
    cluster_name: str,
    node: str,
    kind: str,
    raw: object,
    endpoint_id: int | None = None,
    zone: str | None = None,
    vnet: str | None = None,
) -> SdnNodeStatusSchema:
    data = _to_mapping(raw)
    name = _text(data, "name", "vnet", "zone", "ip", "mac", "route", "subdir")
    return SdnNodeStatusSchema(
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        node=node,
        zone=zone or _text(data, "zone"),
        vnet=vnet or _text(data, "vnet"),
        kind=kind,
        name=name,
        status=_text(data, "status") or "ok",
        raw_config=_raw_dict(data),
    )


async def collect_sdn_inventory_for_session(  # noqa: C901
    px: object,
    *,
    endpoint_id: int | None = None,
    include_node_runtime: bool = True,
) -> SdnInventory:
    """Collect Proxmox SDN inventory for one session without mutating Proxmox."""

    cluster_name = str(getattr(px, "cluster_name", None) or getattr(px, "name", None) or "default")
    endpoint_name = str(getattr(px, "name", None) or cluster_name)
    inventory = SdnInventory(
        endpoint_id=endpoint_id,
        endpoint_name=endpoint_name,
        cluster_name=cluster_name,
    )

    for row in await _fetch_rows(px, "cluster/sdn/controllers", inventory):
        inventory.controllers.append(_to_controller(cluster_name, row, endpoint_id))
    for row in await _fetch_rows(px, "cluster/sdn/zones", inventory):
        inventory.zones.append(_to_zone(cluster_name, row, endpoint_id))
    for row in await _fetch_rows(px, "cluster/sdn/vnets", inventory):
        inventory.vnets.append(_to_vnet(cluster_name, row, endpoint_id))
    for row in await _fetch_rows(px, "cluster/sdn/fabrics", inventory):
        inventory.fabrics.append(_to_fabric(cluster_name, row, endpoint_id))
    for row in await _fetch_rows(px, "cluster/sdn/route-maps", inventory):
        inventory.route_maps.append(_to_route_map(cluster_name, row, endpoint_id))
    for row in await _fetch_rows(px, "cluster/sdn/prefix-lists", inventory):
        inventory.prefix_lists.append(_to_prefix_list(cluster_name, row, endpoint_id))

    vnet_zone = {vnet.vnet: vnet.zone for vnet in inventory.vnets if vnet.vnet}
    for vnet in inventory.vnets:
        if not vnet.vnet:
            continue
        path = f"cluster/sdn/vnets/{vnet.vnet}/subnets"
        for row in await _fetch_rows(px, path, inventory):
            inventory.subnets.append(
                _to_subnet(cluster_name, vnet.vnet, vnet.zone, row, endpoint_id)
            )

    if not include_node_runtime:
        return inventory

    for node in _node_names(px):
        for row in await _fetch_rows(px, f"nodes/{node}/sdn/zones", inventory):
            inventory.node_status.append(
                _to_node_status(
                    cluster_name=cluster_name,
                    node=node,
                    kind="node-zone",
                    raw=row,
                    endpoint_id=endpoint_id,
                )
            )
        for zone in [zone.zone for zone in inventory.zones if zone.zone]:
            for kind, suffix in (
                ("node-zone-bridge", "bridges"),
                ("node-zone-content", "content"),
                ("node-ip-vrf", "ip-vrf"),
            ):
                path = f"nodes/{node}/sdn/zones/{zone}/{suffix}"
                for row in await _fetch_rows(px, path, inventory):
                    inventory.node_status.append(
                        _to_node_status(
                            cluster_name=cluster_name,
                            node=node,
                            zone=zone,
                            kind=kind,
                            raw=row,
                            endpoint_id=endpoint_id,
                        )
                    )
        for vnet in [vnet.vnet for vnet in inventory.vnets if vnet.vnet]:
            path = f"nodes/{node}/sdn/vnets/{vnet}/mac-vrf"
            for row in await _fetch_rows(px, path, inventory):
                inventory.node_status.append(
                    _to_node_status(
                        cluster_name=cluster_name,
                        node=node,
                        zone=vnet_zone.get(vnet),
                        vnet=vnet,
                        kind="node-mac-vrf",
                        raw=row,
                        endpoint_id=endpoint_id,
                    )
                )

    return inventory


async def collect_sdn_inventory(
    pxs: list[object],
    *,
    include_node_runtime: bool = True,
) -> list[SdnInventory]:
    """Collect Proxmox SDN inventory for all sessions."""

    return [
        await collect_sdn_inventory_for_session(px, include_node_runtime=include_node_runtime)
        for px in pxs
    ]


def _identity_normalizer(*fields: str):
    def normalize(record: dict[str, object]) -> dict[str, object]:
        normalized: dict[str, object] = {}
        for field_name in fields:
            value = record.get(field_name)
            if field_name in {
                "export_policies",
                "import_policies",
                "import_targets",
                "export_targets",
                "match_community",
                "match_community_list",
                "match_aspath_list",
                "match_ip_address",
                "match_ipv6_address",
            }:
                value = _relation_ids(value)
            elif field_name in _RELATION_ID_FIELDS:
                value = _relation_id(value)
            normalized[field_name] = value
        return normalized

    return normalize


async def _reconcile(
    nb: object,
    path: str,
    *,
    lookup: dict[str, object],
    payload: dict[str, object],
    fields: tuple[str, ...],
    lookup_query_field_map: dict[str, str] | None = None,
) -> tuple[int | None, bool]:
    result = await rest_reconcile_async_with_status(
        nb,
        path,
        lookup=lookup,
        payload=payload,
        schema=dict,
        current_normalizer=_identity_normalizer(*fields),
        patchable_fields=set(fields),
        lookup_query_field_map=lookup_query_field_map,
    )
    return _relation_id(result.record.serialize()), result.status in {"created", "updated"}


async def _ensure_route_target(nb: object, rt_name: str) -> tuple[int | None, bool]:
    payload = {
        "name": rt_name,
        "description": "Imported from Proxmox SDN EVPN route-target configuration.",
    }
    return await _reconcile(
        nb,
        "/api/ipam/route-targets/",
        lookup={"name": rt_name},
        payload=payload,
        fields=("name", "description"),
    )


async def _ensure_l2vpn(
    nb: object,
    *,
    endpoint_id: int,
    endpoint_name: str,
    cluster_name: str,
    zone: SdnZoneSchema,
    vnet: SdnVNetSchema,
    import_target_ids: list[int],
) -> tuple[int | None, bool]:
    zone_name = zone.zone or "unknown"
    vnet_name = vnet.vnet or "unknown"
    slug = f"proxbox-{endpoint_id}-{_slug(cluster_name)}-{_slug(zone_name)}-{_slug(vnet_name)}"
    payload: dict[str, object] = {
        "name": f"Proxbox {endpoint_name} / {cluster_name} / {zone_name} / {vnet_name}",
        "slug": slug,
        "type": _MANAGED_L2VPN_TYPES[str(zone.type).lower()],
        "status": _netbox_status(vnet.state or zone.state, vnet.pending or zone.pending),
        "description": "Imported from Proxmox SDN VNet configuration.",
        "import_targets": import_target_ids,
        "export_targets": [],
    }
    if vnet.tag is not None:
        payload["identifier"] = vnet.tag
    return await _reconcile(
        nb,
        "/api/vpn/l2vpns/",
        lookup={"slug": slug},
        payload=payload,
        fields=(
            "name",
            "slug",
            "type",
            "status",
            "identifier",
            "description",
            "import_targets",
            "export_targets",
        ),
    )


def _valid_prefix(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return None


async def _ensure_prefix(nb: object, subnet: SdnSubnetSchema) -> tuple[int | None, bool]:
    prefix = _valid_prefix(subnet.subnet)
    if prefix is None:
        return None, False
    payload = {
        "prefix": prefix,
        "status": "active",
        "description": (
            f"Imported from Proxmox SDN VNet {subnet.vnet or 'unknown'} "
            f"on {subnet.cluster_name or 'unknown'}."
        ),
    }
    return await _reconcile(
        nb,
        "/api/ipam/prefixes/",
        lookup={"prefix": prefix},
        payload=payload,
        fields=("prefix", "status", "description"),
    )


def _truncate(value: str, max_length: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def _normalize_sync_mode(value: object, *, param_name: str) -> str:
    normalized = str(value or "disabled").strip().lower()
    if normalized in _VALID_SYNC_MODES:
        return normalized
    logger.warning("Invalid %s=%r; treating it as disabled.", param_name, value)
    return "disabled"


def _sdn_bgp_projection_enabled(sync_mode_sdn_bgp: object) -> bool:
    return _normalize_sync_mode(sync_mode_sdn_bgp, param_name="sync_mode_sdn_bgp") != "disabled"


def _missing_optional_bgp_plugin(error: Exception) -> bool:
    text = str(error).lower()
    if "api/plugins/bgp" not in text and "netbox_bgp" not in text:
        return False
    return any(
        fragment in text
        for fragment in (
            "404",
            "not found",
            "no route",
            "not installed",
            "unknown plugin",
        )
    )


async def _netbox_bgp_available(nb: object) -> bool:
    try:
        await rest_first_async(nb, _BGP_API_PEER_GROUPS, query={"limit": 1})
    except ProxboxException as error:
        if _missing_optional_bgp_plugin(error):
            return False
        raise
    return True


def _valid_community_values(value: object) -> list[str]:
    values: list[str] = []
    for part in re.split(r"[\s,]+", str(value or "")):
        candidate = part.strip()
        if not candidate:
            continue
        match = _BGP_COMMUNITY_RE.fullmatch(candidate)
        if match is None:
            continue
        if int(match.group("left")) > 4_294_967_295:
            continue
        if int(match.group("right")) > 4_294_967_295:
            continue
        values.append(candidate)
    return values


def _bgp_action(value: object) -> str:
    normalized = str(value or "permit").strip().lower()
    if normalized in {"deny", "reject", "drop"}:
        return "deny"
    return "permit"


def _ip_address_candidates(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        if "/" in value:
            interface = ipaddress.ip_interface(value)
        else:
            address = ipaddress.ip_address(value)
            prefix_length = 32 if address.version == 4 else 128
            interface = ipaddress.ip_interface(f"{address}/{prefix_length}")
    except ValueError:
        return []
    return [str(interface)]


async def _resolve_ip_address_id(nb: object, value: str | None) -> int | None:
    for candidate in _ip_address_candidates(value):
        record = await rest_first_async(
            nb,
            "/api/ipam/ip-addresses/",
            query={"address": candidate, "limit": 2},
        )
        record_id = _relation_id(_to_mapping(record)) if record is not None else None
        if record_id is not None:
            return record_id
    return None


async def _resolve_asn_id(nb: object, asn: int | None) -> int | None:
    if asn is None:
        return None
    record = await rest_first_async(
        nb,
        "/api/ipam/asns/",
        query={"asn": asn, "limit": 2},
    )
    return _relation_id(_to_mapping(record)) if record is not None else None


async def _resolve_prefix_id(nb: object, prefix: str | None) -> int | None:
    normalized = _valid_prefix(prefix)
    if normalized is None:
        return None
    record = await rest_first_async(
        nb,
        "/api/ipam/prefixes/",
        query={"prefix": normalized, "limit": 2},
    )
    return _relation_id(_to_mapping(record)) if record is not None else None


def _prefix_family(prefix: str | None) -> str:
    normalized = _valid_prefix(prefix)
    if normalized is None:
        return "ipv4"
    network = ipaddress.ip_network(normalized, strict=False)
    return "ipv6" if network.version == 6 else "ipv4"


def _parse_peer_token(token: str) -> tuple[str, int | None]:
    text = token.strip()
    for delimiter in ("=", "@", "|"):
        if delimiter not in text:
            continue
        peer, raw_asn = text.rsplit(delimiter, 1)
        try:
            return peer.strip(), int(raw_asn.strip())
        except ValueError:
            return text, None
    return text, None


async def _record_bgp_binding_counted(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    source_type: str,
    source_name: str,
    target_kind: str,
    target_id: int | None,
    raw_config: dict[str, object],
) -> None:
    endpoint_id = inventory.endpoint_id
    target_type = _NETBOX_BGP_TARGET_TYPES[target_kind]
    if endpoint_id is None or target_id is None:
        return
    try:
        changed = await _record_binding(
            nb,
            endpoint_id=endpoint_id,
            cluster_name=inventory.cluster_name,
            source_type=source_type,
            source_name=source_name,
            target_type=target_type,
            target_id=target_id,
            raw_config=raw_config,
        )
    except Exception as error:  # noqa: BLE001
        counters.record_object_error(
            inventory.cluster_name,
            kind=f"{source_type}-binding",
            name=source_name,
            error=error,
        )
        logger.warning("Could not record SDN BGP binding %s: %s", source_name, error)
        return
    if changed:
        counters.plugin_metadata += 1


async def _ensure_bgp_community(
    nb: object,
    value: str,
) -> tuple[int | None, bool]:
    payload = {
        "value": value,
        "status": "active",
        "description": "Imported from Proxmox SDN route-map community action.",
    }
    return await _reconcile(
        nb,
        _BGP_API_COMMUNITIES,
        lookup={"value": value},
        payload=payload,
        fields=("value", "status", "description"),
    )


async def _ensure_bgp_prefix_list(
    nb: object,
    inventory: SdnInventory,
    prefix_list: SdnPrefixListSchema,
) -> tuple[int | None, bool, str]:
    family = _prefix_family(prefix_list.cidr)
    name = _truncate(
        f"Proxbox SDN {inventory.endpoint_id or 'unknown'} {inventory.cluster_name} "
        f"{prefix_list.name or 'prefix-list'}",
        100,
    )
    description = _truncate(
        f"Imported from Proxmox SDN prefix-list {prefix_list.name or 'unknown'} "
        f"on {inventory.endpoint_name}.",
        200,
    )
    payload = {
        "name": name,
        "description": description,
        "family": family,
        "comments": "Managed by proxbox-api from read-only Proxmox SDN inventory.",
    }
    prefix_list_id, changed = await _reconcile(
        nb,
        _BGP_API_PREFIX_LISTS,
        lookup={"name": name, "description": description, "family": family},
        payload=payload,
        fields=("name", "description", "family", "comments"),
    )
    return prefix_list_id, changed, family


async def _ensure_bgp_prefix_list_rule(
    nb: object,
    *,
    prefix_list_id: int,
    prefix_list_name: str,
    index: int,
    source: SdnPrefixListSchema,
) -> tuple[int | None, bool]:
    normalized_prefix = _valid_prefix(source.cidr)
    if normalized_prefix is None:
        return None, False
    prefix_id = await _resolve_prefix_id(nb, normalized_prefix)
    payload: dict[str, object] = {
        "prefix_list": prefix_list_id,
        "index": index,
        "action": _bgp_action(source.action),
        "ge": source.ge,
        "le": source.le,
        "description": _truncate(
            f"Imported from Proxmox SDN prefix-list {prefix_list_name}.",
            200,
        ),
        "comments": "Managed by proxbox-api from read-only Proxmox SDN inventory.",
    }
    fields = (
        "prefix_list",
        "index",
        "action",
        "prefix",
        "prefix_custom",
        "ge",
        "le",
        "description",
        "comments",
    )
    if prefix_id is not None:
        payload["prefix"] = prefix_id
        payload["prefix_custom"] = None
    else:
        payload["prefix"] = None
        payload["prefix_custom"] = normalized_prefix
    return await _reconcile(
        nb,
        _BGP_API_PREFIX_LIST_RULES,
        lookup={"prefix_list_id": prefix_list_id, "index": index},
        payload=payload,
        fields=fields,
    )


async def _ensure_bgp_routing_policy(
    nb: object,
    inventory: SdnInventory,
    route_map: SdnRouteMapSchema,
) -> tuple[int | None, bool]:
    name = _truncate(
        f"Proxbox SDN {inventory.endpoint_id or 'unknown'} {inventory.cluster_name} "
        f"{route_map.name or 'route-map'}",
        100,
    )
    description = _truncate(
        f"Imported from Proxmox SDN route-map {route_map.name or 'unknown'} "
        f"on {inventory.endpoint_name}.",
        200,
    )
    payload = {
        "name": name,
        "description": description,
        "weight": route_map.order,
        "comments": "Managed by proxbox-api from read-only Proxmox SDN inventory.",
    }
    return await _reconcile(
        nb,
        _BGP_API_ROUTING_POLICIES,
        lookup={"name": name, "description": description},
        payload=payload,
        fields=("name", "description", "weight", "comments"),
    )


async def _ensure_bgp_routing_policy_rule(
    nb: object,
    *,
    routing_policy_id: int,
    index: int,
    route_map: SdnRouteMapSchema,
    match_ipv4_prefix_lists: list[int],
    match_ipv6_prefix_lists: list[int],
    community_values: list[str],
) -> tuple[int | None, bool]:
    match_custom = {
        key: value
        for key, value in {
            "match_peer": route_map.match_peer,
            "match_ip": route_map.match_ip,
        }.items()
        if value
    }
    set_actions = {"communities": community_values} if community_values else None
    payload = {
        "routing_policy": routing_policy_id,
        "index": index,
        "action": _bgp_action(route_map.action),
        "match_ip_address": match_ipv4_prefix_lists,
        "match_ipv6_address": match_ipv6_prefix_lists,
        "match_custom": match_custom or None,
        "set_actions": set_actions,
        "description": _truncate(
            f"Imported from Proxmox SDN route-map {route_map.name or 'unknown'}.",
            500,
        ),
        "comments": "Managed by proxbox-api from read-only Proxmox SDN inventory.",
    }
    return await _reconcile(
        nb,
        _BGP_API_ROUTING_POLICY_RULES,
        lookup={"routing_policy_id": routing_policy_id, "index": index},
        payload=payload,
        fields=(
            "routing_policy",
            "index",
            "action",
            "match_ip_address",
            "match_ipv6_address",
            "match_custom",
            "set_actions",
            "description",
            "comments",
        ),
    )


async def _ensure_bgp_peer_group(
    nb: object,
    inventory: SdnInventory,
    *,
    source_name: str,
    source_kind: str,
    raw_config: dict[str, object],
) -> tuple[int | None, bool]:
    name = _truncate(
        f"Proxbox SDN {inventory.endpoint_id or 'unknown'} {inventory.cluster_name} {source_name}",
        100,
    )
    description = _truncate(
        f"Imported from Proxmox SDN {source_kind} {source_name} on {inventory.endpoint_name}.",
        200,
    )
    payload = {
        "name": name,
        "description": description,
        "comments": (
            "Managed by proxbox-api from read-only Proxmox SDN inventory.\n"
            f"Source: {source_kind}\n"
            f"Raw: {raw_config}"
        ),
    }
    return await _reconcile(
        nb,
        _BGP_API_PEER_GROUPS,
        lookup={"name": name, "description": description},
        payload=payload,
        fields=("name", "description", "comments"),
    )


async def _ensure_bgp_session(
    nb: object,
    *,
    name: str,
    local_address_id: int,
    remote_address_id: int,
    local_as_id: int,
    remote_as_id: int,
    peer_group_id: int | None,
    description: str,
) -> tuple[int | None, bool]:
    payload: dict[str, object] = {
        "name": name,
        "status": "active",
        "local_address": local_address_id,
        "remote_address": remote_address_id,
        "local_as": local_as_id,
        "remote_as": remote_as_id,
        "peer_group": peer_group_id,
        "description": description,
        "comments": "Managed by proxbox-api from read-only Proxmox SDN inventory.",
    }
    return await _reconcile(
        nb,
        _BGP_API_SESSIONS,
        lookup={"name": name},
        payload=payload,
        fields=(
            "name",
            "status",
            "local_address",
            "remote_address",
            "local_as",
            "remote_as",
            "peer_group",
            "description",
            "comments",
        ),
    )


async def _ensure_l2vpn_termination(
    nb: object,
    *,
    l2vpn_id: int,
    target_type: str,
    target_id: int,
) -> tuple[int | None, bool, str | None]:
    existing = await rest_first_async(
        nb,
        "/api/vpn/l2vpn-terminations/",
        query={
            "assigned_object_type": target_type,
            "assigned_object_id": target_id,
            "limit": 2,
        },
    )
    if existing is not None:
        existing_data = _to_mapping(existing)
        existing_l2vpn_id = _relation_id(existing_data.get("l2vpn"))
        if existing_l2vpn_id not in {None, l2vpn_id}:
            return (
                _relation_id(existing_data),
                False,
                f"Target already terminates L2VPN {existing_l2vpn_id}.",
            )

    termination_id, changed = await _reconcile(
        nb,
        "/api/vpn/l2vpn-terminations/",
        lookup={
            "l2vpn": l2vpn_id,
            "assigned_object_type": target_type,
            "assigned_object_id": target_id,
        },
        payload={
            "l2vpn": l2vpn_id,
            "assigned_object_type": target_type,
            "assigned_object_id": target_id,
        },
        fields=("l2vpn", "assigned_object_type", "assigned_object_id"),
    )
    return termination_id, changed, None


async def _plugin_upsert(
    nb: object,
    path: str,
    *,
    lookup: dict[str, object],
    payload: dict[str, object],
    fields: tuple[str, ...],
) -> bool:
    _, changed = await _reconcile(
        nb,
        path,
        lookup=lookup,
        payload=payload,
        fields=fields,
    )
    return changed


async def _plugin_upsert_counted(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    kind: str,
    name: str,
    path: str,
    lookup: dict[str, object],
    payload: dict[str, object],
    fields: tuple[str, ...],
) -> None:
    try:
        changed = await _plugin_upsert(
            nb,
            path,
            lookup=lookup,
            payload=payload,
            fields=fields,
        )
    except Exception as error:  # noqa: BLE001
        counters.record_object_error(
            inventory.cluster_name,
            kind=kind,
            name=name or path,
            error=error,
        )
        logger.warning("Could not sync SDN plugin %s %s: %s", kind, name or path, error)
        return
    if changed:
        counters.plugin_metadata += 1


async def _sync_plugin_inventory(  # noqa: C901
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    vnet_l2vpn_ids: dict[tuple[str, str], int],
    subnet_prefix_ids: dict[tuple[str, str], int],
) -> None:
    endpoint_id = inventory.endpoint_id
    if endpoint_id is None:
        counters.skipped += 1
        return

    for controller in inventory.controllers:
        if not controller.controller:
            counters.skipped += 1
            continue
        await _plugin_upsert_counted(
            nb,
            inventory,
            counters,
            kind="controller",
            name=controller.controller,
            path="/api/plugins/proxbox/sdn-controllers/",
            lookup={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "controller_name": controller.controller,
            },
            payload={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "controller_name": controller.controller,
                "controller_type": controller.type or "",
                "asn": controller.asn,
                "peers": _split_csv(controller.peers),
                "nodes": _split_csv(controller.node),
                "loopback": controller.loopback or "",
                "state": controller.state or "",
                "status": "active",
                "raw_config": controller.raw_config,
            },
            fields=(
                "endpoint",
                "cluster_name",
                "controller_name",
                "controller_type",
                "asn",
                "peers",
                "nodes",
                "loopback",
                "state",
                "status",
                "raw_config",
            ),
        )

    for fabric in inventory.fabrics:
        if not fabric.fabric:
            counters.skipped += 1
            continue
        await _plugin_upsert_counted(
            nb,
            inventory,
            counters,
            kind="fabric",
            name=fabric.fabric,
            path="/api/plugins/proxbox/sdn-fabrics/",
            lookup={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "fabric_name": fabric.fabric,
            },
            payload={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "fabric_name": fabric.fabric,
                "fabric_type": fabric.type or "unknown",
                "asn": fabric.asn,
                "advertise_subnets": bool(fabric.advertise_subnets),
                "disable_arp_nd_suppression": bool(fabric.disable_arp_nd_suppression),
                "vrf_vxlan": fabric.vrf_vxlan,
                "peers": _split_csv(fabric.peers),
                "status": "active",
                "raw_config": fabric.raw_config,
            },
            fields=(
                "endpoint",
                "cluster_name",
                "fabric_name",
                "fabric_type",
                "asn",
                "advertise_subnets",
                "disable_arp_nd_suppression",
                "vrf_vxlan",
                "peers",
                "status",
                "raw_config",
            ),
        )

    for route_map in inventory.route_maps:
        if not route_map.name:
            counters.skipped += 1
            continue
        await _plugin_upsert_counted(
            nb,
            inventory,
            counters,
            kind="route-map",
            name=route_map.name,
            path="/api/plugins/proxbox/sdn-route-maps/",
            lookup={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "name": route_map.name,
                "order": route_map.order or 0,
            },
            payload={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "name": route_map.name,
                "action": route_map.action or "",
                "match_peer": route_map.match_peer or "",
                "match_ip": route_map.match_ip or "",
                "set_community": route_map.set_community or "",
                "order": route_map.order or 0,
                "status": "active",
                "raw_config": route_map.raw_config,
            },
            fields=(
                "endpoint",
                "cluster_name",
                "name",
                "action",
                "match_peer",
                "match_ip",
                "set_community",
                "order",
                "status",
                "raw_config",
            ),
        )

    for prefix_list in inventory.prefix_lists:
        if not prefix_list.name:
            counters.skipped += 1
            continue
        await _plugin_upsert_counted(
            nb,
            inventory,
            counters,
            kind="prefix-list",
            name=prefix_list.name,
            path="/api/plugins/proxbox/sdn-prefix-lists/",
            lookup={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "name": prefix_list.name,
                "cidr": prefix_list.cidr or "",
            },
            payload={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "name": prefix_list.name,
                "cidr": prefix_list.cidr or "",
                "action": prefix_list.action or "",
                "le": prefix_list.le,
                "ge": prefix_list.ge,
                "status": "active",
                "raw_config": prefix_list.raw_config,
            },
            fields=(
                "endpoint",
                "cluster_name",
                "name",
                "cidr",
                "action",
                "le",
                "ge",
                "status",
                "raw_config",
            ),
        )

    for zone in inventory.zones:
        if not zone.zone:
            counters.skipped += 1
            continue
        await _plugin_upsert_counted(
            nb,
            inventory,
            counters,
            kind="zone",
            name=zone.zone,
            path="/api/plugins/proxbox/sdn-zones/",
            lookup={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "zone_name": zone.zone,
            },
            payload={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "zone_name": zone.zone,
                "zone_type": zone.type or "",
                "controller": zone.controller or "",
                "vrf_vxlan": zone.vrf_vxlan,
                "tag": zone.tag,
                "mtu": zone.mtu,
                "dns": zone.dns or "",
                "ipam": zone.ipam or "",
                "rt_import": _split_csv(zone.rt_import),
                "state": zone.state or "",
                "status": "active",
                "raw_config": zone.raw_config,
            },
            fields=(
                "endpoint",
                "cluster_name",
                "zone_name",
                "zone_type",
                "controller",
                "vrf_vxlan",
                "tag",
                "mtu",
                "dns",
                "ipam",
                "rt_import",
                "state",
                "status",
                "raw_config",
            ),
        )

    for vnet in inventory.vnets:
        if not vnet.vnet:
            counters.skipped += 1
            continue
        l2vpn_id = vnet_l2vpn_ids.get((vnet.zone or "", vnet.vnet))
        await _plugin_upsert_counted(
            nb,
            inventory,
            counters,
            kind="vnet",
            name=vnet.vnet,
            path="/api/plugins/proxbox/sdn-vnets/",
            lookup={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "vnet_name": vnet.vnet,
            },
            payload={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "zone_name": vnet.zone or "",
                "vnet_name": vnet.vnet,
                "vnet_type": vnet.type or "",
                "tag": vnet.tag,
                "alias": vnet.alias or "",
                "vlanaware": bool(vnet.vlanaware),
                "state": vnet.state or "",
                "l2vpn": l2vpn_id,
                "status": "active",
                "raw_config": vnet.raw_config,
            },
            fields=(
                "endpoint",
                "cluster_name",
                "zone_name",
                "vnet_name",
                "vnet_type",
                "tag",
                "alias",
                "vlanaware",
                "state",
                "l2vpn",
                "status",
                "raw_config",
            ),
        )

    for subnet in inventory.subnets:
        if not subnet.vnet or not subnet.subnet:
            counters.skipped += 1
            continue
        prefix_id = subnet_prefix_ids.get((subnet.vnet, subnet.subnet))
        skip_reason = "" if prefix_id else "Invalid or ambiguous subnet payload."
        if prefix_id is None:
            counters.skipped += 1
        await _plugin_upsert_counted(
            nb,
            inventory,
            counters,
            kind="subnet",
            name=subnet.subnet,
            path="/api/plugins/proxbox/sdn-subnets/",
            lookup={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "vnet_name": subnet.vnet,
                "subnet": subnet.subnet,
            },
            payload={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "zone_name": subnet.zone or "",
                "vnet_name": subnet.vnet,
                "subnet": subnet.subnet,
                "subnet_type": subnet.type or "",
                "gateway": subnet.gateway or "",
                "snat": bool(subnet.snat),
                "prefix": prefix_id,
                "skip_reason": skip_reason,
                "status": "active",
                "raw_config": subnet.raw_config,
            },
            fields=(
                "endpoint",
                "cluster_name",
                "zone_name",
                "vnet_name",
                "subnet",
                "subnet_type",
                "gateway",
                "snat",
                "prefix",
                "skip_reason",
                "status",
                "raw_config",
            ),
        )

    for row in inventory.node_status:
        source = "/".join(
            part for part in (row.node, row.zone, row.vnet, row.kind, row.name) if part
        )
        if not source:
            counters.skipped += 1
            continue
        await _plugin_upsert_counted(
            nb,
            inventory,
            counters,
            kind="binding",
            name=source,
            path="/api/plugins/proxbox/sdn-bindings/",
            lookup={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "source_type": row.kind,
                "source_name": source,
            },
            payload={
                "endpoint": endpoint_id,
                "cluster_name": inventory.cluster_name,
                "source_type": row.kind,
                "source_name": source,
                "node": row.node or "",
                "zone_name": row.zone or "",
                "vnet_name": row.vnet or "",
                "target_type": "",
                "target_id": None,
                "status": row.status or "active",
                "conflict_reason": row.error or "",
                "raw_config": row.raw_config,
            },
            fields=(
                "endpoint",
                "cluster_name",
                "source_type",
                "source_name",
                "node",
                "zone_name",
                "vnet_name",
                "target_type",
                "target_id",
                "status",
                "conflict_reason",
                "raw_config",
            ),
        )


async def _sync_netbox_l2vpn_objects(  # noqa: C901
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], int]]:
    vnet_l2vpn_ids: dict[tuple[str, str], int] = {}
    subnet_prefix_ids: dict[tuple[str, str], int] = {}

    endpoint_id = inventory.endpoint_id
    if endpoint_id is None:
        counters.skipped += len(inventory.vnets)
        return vnet_l2vpn_ids, subnet_prefix_ids

    zones_by_name = {zone.zone: zone for zone in inventory.zones if zone.zone}

    for vnet in inventory.vnets:
        if not vnet.vnet or not vnet.zone:
            counters.skipped += 1
            continue
        zone = zones_by_name.get(vnet.zone)
        if zone is None or str(zone.type or "").lower() not in _MANAGED_L2VPN_TYPES:
            counters.skipped += 1
            continue

        import_target_ids: list[int] = []
        for rt_name in _split_csv(zone.rt_import):
            try:
                rt_id, changed = await _ensure_route_target(nb, rt_name)
            except Exception as error:  # noqa: BLE001
                counters.record_object_error(
                    inventory.cluster_name,
                    kind="route-target",
                    name=rt_name,
                    error=error,
                )
                logger.warning("Could not sync RouteTarget %s: %s", rt_name, error)
                continue
            if rt_id is not None:
                import_target_ids.append(rt_id)
                if changed:
                    counters.route_targets += 1

        try:
            l2vpn_id, changed = await _ensure_l2vpn(
                nb,
                endpoint_id=endpoint_id,
                endpoint_name=inventory.endpoint_name,
                cluster_name=inventory.cluster_name,
                zone=zone,
                vnet=vnet,
                import_target_ids=import_target_ids,
            )
        except Exception as error:  # noqa: BLE001
            counters.record_object_error(
                inventory.cluster_name,
                kind="l2vpn",
                name=f"{vnet.zone}/{vnet.vnet}",
                error=error,
            )
            logger.warning("Could not sync L2VPN for %s/%s: %s", vnet.zone, vnet.vnet, error)
            continue
        if l2vpn_id is not None:
            vnet_l2vpn_ids[(vnet.zone, vnet.vnet)] = l2vpn_id
            if changed:
                counters.l2vpns += 1

    for subnet in inventory.subnets:
        if not subnet.vnet or not subnet.subnet:
            counters.skipped += 1
            continue
        prefix = _valid_prefix(subnet.subnet)
        if prefix is None:
            counters.skipped += 1
            continue
        try:
            prefix_id, changed = await _ensure_prefix(nb, subnet)
        except Exception as error:  # noqa: BLE001
            counters.record_object_error(
                inventory.cluster_name,
                kind="prefix",
                name=subnet.subnet,
                error=error,
            )
            logger.warning("Could not sync Prefix for SDN subnet %s: %s", subnet.subnet, error)
            continue
        if prefix_id is not None:
            subnet_prefix_ids[(subnet.vnet, subnet.subnet)] = prefix_id
            if changed:
                counters.subnets += 1

    return vnet_l2vpn_ids, subnet_prefix_ids


async def _record_binding(
    nb: object,
    *,
    endpoint_id: int,
    cluster_name: str,
    source_type: str,
    source_name: str,
    target_type: str,
    target_id: int | None,
    raw_config: dict[str, object],
    status: str = "active",
    conflict_reason: str = "",
) -> bool:
    return await _plugin_upsert(
        nb,
        "/api/plugins/proxbox/sdn-bindings/",
        lookup={
            "endpoint": endpoint_id,
            "cluster_name": cluster_name,
            "source_type": source_type,
            "source_name": source_name,
        },
        payload={
            "endpoint": endpoint_id,
            "cluster_name": cluster_name,
            "source_type": source_type,
            "source_name": source_name,
            "node": "",
            "zone_name": "",
            "vnet_name": "",
            "target_type": target_type,
            "target_id": target_id,
            "status": status,
            "conflict_reason": conflict_reason,
            "raw_config": raw_config,
        },
        fields=(
            "endpoint",
            "cluster_name",
            "source_type",
            "source_name",
            "node",
            "zone_name",
            "vnet_name",
            "target_type",
            "target_id",
            "status",
            "conflict_reason",
            "raw_config",
        ),
    )


async def _record_termination_binding_counted(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    endpoint_id: int,
    source_name: str,
    target_type: str,
    target_id: int,
    raw_config: dict[str, object],
    status: str = "active",
    conflict_reason: str = "",
) -> bool:
    try:
        return await _record_binding(
            nb,
            endpoint_id=endpoint_id,
            cluster_name=inventory.cluster_name,
            source_type="l2vpn-termination",
            source_name=source_name,
            target_type=target_type,
            target_id=target_id,
            status=status,
            conflict_reason=conflict_reason,
            raw_config=raw_config,
        )
    except Exception as error:  # noqa: BLE001
        counters.record_object_error(
            inventory.cluster_name,
            kind="l2vpn-termination-binding",
            name=source_name,
            error=error,
        )
        logger.warning("Could not record SDN termination binding %s: %s", source_name, error)
        return False


async def _resolve_termination_target(
    nb: object,
    row: SdnNodeStatusSchema,
) -> tuple[str, int] | None:
    raw = row.raw_config
    explicit_type = _content_type_value(
        _value(raw, "target_type", "assigned_object_type", "target-content-type")
    )
    explicit_id = _int(raw, "target_id", "assigned_object_id", "target-object-id")
    if explicit_type in _TERMINATION_TARGET_TYPES and explicit_id is not None and explicit_id > 0:
        return explicit_type, explicit_id

    vlan_id = _int(raw, "vid", "vlan")
    if vlan_id is None or vlan_id < 1 or vlan_id > 4094:
        return None
    vlan = await rest_first_async(
        nb,
        "/api/ipam/vlans/",
        query={"vid": vlan_id, "limit": 2},
    )
    vlan_record_id = _relation_id(_to_mapping(vlan)) if vlan is not None else None
    if vlan_record_id is None:
        return None
    return "ipam.vlan", vlan_record_id


def _l2vpn_id_for_row(
    row: SdnNodeStatusSchema,
    vnet_l2vpn_ids: dict[tuple[str, str], int],
) -> int | None:
    if row.zone and row.vnet:
        return vnet_l2vpn_ids.get((row.zone, row.vnet))
    if not row.vnet:
        return None
    matches = [
        l2vpn_id
        for (_zone_name, vnet_name), l2vpn_id in vnet_l2vpn_ids.items()
        if vnet_name == row.vnet
    ]
    return matches[0] if len(matches) == 1 else None


async def _sync_l2vpn_terminations(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    vnet_l2vpn_ids: dict[tuple[str, str], int],
) -> None:
    endpoint_id = inventory.endpoint_id
    if endpoint_id is None:
        return

    for row in inventory.node_status:
        l2vpn_id = _l2vpn_id_for_row(row, vnet_l2vpn_ids)
        if l2vpn_id is None:
            continue
        target = await _resolve_termination_target(nb, row)
        if target is None:
            continue
        target_type, target_id = target
        source_name = "/".join(
            part
            for part in (
                row.node,
                row.zone,
                row.vnet,
                row.kind,
                row.name,
                target_type,
                str(target_id),
            )
            if part
        )
        try:
            termination_id, changed, conflict_reason = await _ensure_l2vpn_termination(
                nb,
                l2vpn_id=l2vpn_id,
                target_type=target_type,
                target_id=target_id,
            )
        except Exception as error:  # noqa: BLE001
            counters.record_object_error(
                inventory.cluster_name,
                kind="l2vpn-termination",
                name=source_name,
                error=error,
            )
            logger.warning(
                "Could not sync L2VPN termination for %s/%s: %s",
                row.vnet,
                target_id,
                error,
            )
            continue
        if conflict_reason:
            counters.skipped += 1
            binding_changed = await _record_termination_binding_counted(
                nb,
                inventory,
                counters,
                endpoint_id=endpoint_id,
                source_name=source_name,
                target_type=target_type,
                target_id=target_id,
                status="conflict",
                conflict_reason=conflict_reason,
                raw_config={
                    **row.raw_config,
                    "l2vpn": l2vpn_id,
                    "termination": termination_id,
                },
            )
            if binding_changed:
                counters.plugin_metadata += 1
            continue
        if changed:
            counters.terminations += 1
        binding_changed = await _record_termination_binding_counted(
            nb,
            inventory,
            counters,
            endpoint_id=endpoint_id,
            source_name=source_name,
            target_type=target_type,
            target_id=target_id,
            raw_config={**row.raw_config, "l2vpn": l2vpn_id, "termination": termination_id},
        )
        if binding_changed:
            counters.plugin_metadata += 1


async def _record_object_bindings(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    vnet_l2vpn_ids: dict[tuple[str, str], int],
    subnet_prefix_ids: dict[tuple[str, str], int],
) -> None:
    endpoint_id = inventory.endpoint_id
    if endpoint_id is None:
        return
    for (zone_name, vnet_name), l2vpn_id in vnet_l2vpn_ids.items():
        changed = await _record_binding(
            nb,
            endpoint_id=endpoint_id,
            cluster_name=inventory.cluster_name,
            source_type="vnet",
            source_name=f"{zone_name}/{vnet_name}",
            target_type="vpn.l2vpn",
            target_id=l2vpn_id,
            raw_config={"zone": zone_name, "vnet": vnet_name},
        )
        if changed:
            counters.plugin_metadata += 1
    for (vnet_name, subnet), prefix_id in subnet_prefix_ids.items():
        changed = await _record_binding(
            nb,
            endpoint_id=endpoint_id,
            cluster_name=inventory.cluster_name,
            source_type="subnet",
            source_name=f"{vnet_name}/{subnet}",
            target_type="ipam.prefix",
            target_id=prefix_id,
            raw_config={"vnet": vnet_name, "subnet": subnet},
        )
        if changed:
            counters.plugin_metadata += 1


async def _sync_bgp_prefix_lists(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
) -> dict[str, tuple[int, str]]:
    prefix_lists: dict[str, tuple[int, str]] = {}
    grouped: dict[str, list[SdnPrefixListSchema]] = {}
    for row in inventory.prefix_lists:
        if not row.name:
            counters.skipped += 1
            continue
        grouped.setdefault(row.name, []).append(row)

    for source_name, rows in grouped.items():
        first = rows[0]
        try:
            prefix_list_id, changed, family = await _ensure_bgp_prefix_list(
                nb,
                inventory,
                first,
            )
        except Exception as error:  # noqa: BLE001
            counters.record_object_error(
                inventory.cluster_name,
                kind="bgp-prefix-list",
                name=source_name,
                error=error,
            )
            logger.warning("Could not sync BGP prefix-list %s: %s", source_name, error)
            continue
        if prefix_list_id is None:
            counters.skipped += 1
            continue
        prefix_lists[source_name] = (prefix_list_id, family)
        if changed:
            counters.bgp_prefix_lists += 1
        await _record_bgp_binding_counted(
            nb,
            inventory,
            counters,
            source_type="bgp-prefix-list",
            source_name=source_name,
            target_kind="prefix_list",
            target_id=prefix_list_id,
            raw_config=first.raw_config,
        )

        await _sync_bgp_prefix_list_rules(
            nb,
            inventory,
            counters,
            prefix_list_id=prefix_list_id,
            source_name=source_name,
            rows=rows,
        )

    return prefix_lists


async def _sync_bgp_prefix_list_rules(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    prefix_list_id: int,
    source_name: str,
    rows: list[SdnPrefixListSchema],
) -> None:
    for index, row in enumerate(rows, start=1):
        rule_index = index * 10
        rule_name = f"{source_name}/{row.cidr or rule_index}"
        if _valid_prefix(row.cidr) is None:
            counters.record_object_warning(
                kind="bgp-prefix-list-rule",
                name=rule_name,
                warning="Prefix-list rule skipped because cidr is invalid or missing.",
            )
            counters.skipped += 1
            continue
        try:
            rule_id, rule_changed = await _ensure_bgp_prefix_list_rule(
                nb,
                prefix_list_id=prefix_list_id,
                prefix_list_name=source_name,
                index=rule_index,
                source=row,
            )
        except Exception as error:  # noqa: BLE001
            counters.record_object_error(
                inventory.cluster_name,
                kind="bgp-prefix-list-rule",
                name=rule_name,
                error=error,
            )
            logger.warning(
                "Could not sync BGP prefix-list rule %s/%s: %s",
                source_name,
                row.cidr,
                error,
            )
            continue
        if rule_id is not None and rule_changed:
            counters.bgp_prefix_list_rules += 1
        await _record_bgp_binding_counted(
            nb,
            inventory,
            counters,
            source_type="bgp-prefix-list-rule",
            source_name=f"{source_name}/{rule_index}",
            target_kind="prefix_list_rule",
            target_id=rule_id,
            raw_config=row.raw_config,
        )


def _matched_prefix_lists(
    match_ip: str | None,
    prefix_lists: dict[str, tuple[int, str]],
) -> tuple[list[int], list[int]]:
    ipv4_ids: list[int] = []
    ipv6_ids: list[int] = []
    for token in _split_csv(match_ip):
        matched = prefix_lists.get(token)
        if matched is None:
            continue
        prefix_list_id, family = matched
        if family == "ipv6":
            ipv6_ids.append(prefix_list_id)
        else:
            ipv4_ids.append(prefix_list_id)
    return ipv4_ids, ipv6_ids


async def _sync_bgp_route_maps(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    prefix_lists: dict[str, tuple[int, str]],
) -> dict[str, int]:
    routing_policy_ids: dict[str, int] = {}
    for index, route_map in enumerate(inventory.route_maps, start=1):
        if not route_map.name:
            counters.skipped += 1
            continue
        try:
            policy_id, changed = await _ensure_bgp_routing_policy(
                nb,
                inventory,
                route_map,
            )
        except Exception as error:  # noqa: BLE001
            counters.record_object_error(
                inventory.cluster_name,
                kind="bgp-routing-policy",
                name=route_map.name,
                error=error,
            )
            logger.warning("Could not sync BGP routing policy %s: %s", route_map.name, error)
            continue
        if policy_id is None:
            counters.skipped += 1
            continue
        routing_policy_ids[route_map.name] = policy_id
        if changed:
            counters.bgp_routing_policies += 1
        await _record_bgp_binding_counted(
            nb,
            inventory,
            counters,
            source_type="bgp-routing-policy",
            source_name=route_map.name,
            target_kind="routing_policy",
            target_id=policy_id,
            raw_config=route_map.raw_config,
        )

        community_values = await _sync_bgp_route_map_communities(
            nb,
            inventory,
            counters,
            route_map=route_map,
        )
        await _sync_bgp_route_map_rule(
            nb,
            inventory,
            counters,
            route_map=route_map,
            routing_policy_id=policy_id,
            rule_index=route_map.order or index * 10,
            prefix_lists=prefix_lists,
            community_values=community_values,
        )

    return routing_policy_ids


async def _sync_bgp_route_map_communities(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    route_map: SdnRouteMapSchema,
) -> list[str]:
    community_values = _valid_community_values(route_map.set_community)
    for value in community_values:
        try:
            community_id, community_changed = await _ensure_bgp_community(nb, value)
        except Exception as error:  # noqa: BLE001
            counters.record_object_error(
                inventory.cluster_name,
                kind="bgp-community",
                name=value,
                error=error,
            )
            logger.warning("Could not sync BGP community %s: %s", value, error)
            continue
        if community_id is not None and community_changed:
            counters.bgp_communities += 1
        await _record_bgp_binding_counted(
            nb,
            inventory,
            counters,
            source_type="bgp-community",
            source_name=value,
            target_kind="community",
            target_id=community_id,
            raw_config={"set_community": route_map.set_community},
        )
    return community_values


async def _sync_bgp_route_map_rule(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    route_map: SdnRouteMapSchema,
    routing_policy_id: int,
    rule_index: int,
    prefix_lists: dict[str, tuple[int, str]],
    community_values: list[str],
) -> None:
    match_ipv4_ids, match_ipv6_ids = _matched_prefix_lists(
        route_map.match_ip,
        prefix_lists,
    )
    try:
        rule_id, rule_changed = await _ensure_bgp_routing_policy_rule(
            nb,
            routing_policy_id=routing_policy_id,
            index=rule_index,
            route_map=route_map,
            match_ipv4_prefix_lists=match_ipv4_ids,
            match_ipv6_prefix_lists=match_ipv6_ids,
            community_values=community_values,
        )
    except Exception as error:  # noqa: BLE001
        counters.record_object_error(
            inventory.cluster_name,
            kind="bgp-routing-policy-rule",
            name=route_map.name or str(rule_index),
            error=error,
        )
        logger.warning("Could not sync BGP routing policy rule %s: %s", route_map.name, error)
        return
    if rule_id is not None and rule_changed:
        counters.bgp_routing_policy_rules += 1
    await _record_bgp_binding_counted(
        nb,
        inventory,
        counters,
        source_type="bgp-routing-policy-rule",
        source_name=f"{route_map.name}/{rule_index}",
        target_kind="routing_policy_rule",
        target_id=rule_id,
        raw_config=route_map.raw_config,
    )


def _iter_bgp_peer_group_sources(
    inventory: SdnInventory,
) -> list[tuple[str, str, int | None, str | None, str | None, dict[str, object]]]:
    sources: list[tuple[str, str, int | None, str | None, str | None, dict[str, object]]] = []
    for controller in inventory.controllers:
        source_name = controller.controller
        if not source_name:
            continue
        if str(controller.type or "").lower() not in {"bgp", "evpn"}:
            continue
        sources.append(
            (
                "controller",
                source_name,
                controller.asn,
                controller.peers,
                controller.loopback,
                controller.raw_config,
            )
        )
    for fabric in inventory.fabrics:
        source_name = fabric.fabric
        if not source_name:
            continue
        if str(fabric.type or "").lower() not in {"bgp", "evpn"}:
            continue
        sources.append(
            (
                "fabric",
                source_name,
                fabric.asn,
                fabric.peers,
                _text(fabric.raw_config, "loopback", "local-address", "local_address"),
                fabric.raw_config,
            )
        )
    return sources


async def _sync_bgp_peer_groups_and_sessions(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
) -> None:
    for (
        source_kind,
        source_name,
        local_asn,
        peers,
        local_address,
        raw_config,
    ) in _iter_bgp_peer_group_sources(inventory):
        try:
            peer_group_id, changed = await _ensure_bgp_peer_group(
                nb,
                inventory,
                source_name=source_name,
                source_kind=source_kind,
                raw_config=raw_config,
            )
        except Exception as error:  # noqa: BLE001
            counters.record_object_error(
                inventory.cluster_name,
                kind="bgp-peer-group",
                name=source_name,
                error=error,
            )
            logger.warning("Could not sync BGP peer group %s: %s", source_name, error)
            continue
        if peer_group_id is not None and changed:
            counters.bgp_peer_groups += 1
        await _record_bgp_binding_counted(
            nb,
            inventory,
            counters,
            source_type="bgp-peer-group",
            source_name=f"{source_kind}/{source_name}",
            target_kind="peer_group",
            target_id=peer_group_id,
            raw_config=raw_config,
        )

        local_as_id = await _resolve_asn_id(nb, local_asn)
        local_address_id = await _resolve_ip_address_id(nb, local_address)
        for peer in _split_csv(peers):
            remote_address, parsed_remote_asn = _parse_peer_token(peer)
            remote_asn = parsed_remote_asn or _int(
                raw_config,
                "remote-as",
                "remote_as",
                "peer-as",
                "peer_as",
            )
            remote_address_id = await _resolve_ip_address_id(nb, remote_address)
            remote_as_id = await _resolve_asn_id(nb, remote_asn)
            missing = [
                name
                for name, value in {
                    "local_address": local_address_id,
                    "remote_address": remote_address_id,
                    "local_as": local_as_id,
                    "remote_as": remote_as_id,
                }.items()
                if value is None
            ]
            session_name = _truncate(
                f"Proxbox SDN {inventory.endpoint_id or 'unknown'} {source_name} {remote_address}",
                256,
            )
            if missing:
                counters.skipped += 1
                counters.record_object_warning(
                    kind="bgp-session",
                    name=session_name,
                    warning=f"Skipped unresolved NetBox reference(s): {', '.join(missing)}.",
                )
                continue
            try:
                session_id, session_changed = await _ensure_bgp_session(
                    nb,
                    name=session_name,
                    local_address_id=local_address_id,
                    remote_address_id=remote_address_id,
                    local_as_id=local_as_id,
                    remote_as_id=remote_as_id,
                    peer_group_id=peer_group_id,
                    description=_truncate(
                        f"Imported from Proxmox SDN {source_kind} {source_name}.",
                        200,
                    ),
                )
            except Exception as error:  # noqa: BLE001
                counters.record_object_error(
                    inventory.cluster_name,
                    kind="bgp-session",
                    name=session_name,
                    error=error,
                )
                logger.warning("Could not sync BGP session %s: %s", session_name, error)
                continue
            if session_id is not None and session_changed:
                counters.bgp_sessions += 1
            await _record_bgp_binding_counted(
                nb,
                inventory,
                counters,
                source_type="bgp-session",
                source_name=session_name,
                target_kind="session",
                target_id=session_id,
                raw_config={
                    **raw_config,
                    "peer": peer,
                    "local_address": local_address,
                    "remote_address": remote_address,
                    "local_asn": local_asn,
                    "remote_asn": remote_asn,
                },
            )


async def _sync_netbox_bgp_projection(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
) -> None:
    if inventory.endpoint_id is None:
        counters.skipped += 1
        counters.record_object_warning(
            kind="bgp-projection",
            name=inventory.cluster_name,
            warning="Skipped because the Proxbox endpoint id could not be resolved.",
        )
        return
    prefix_lists = await _sync_bgp_prefix_lists(nb, inventory, counters)
    await _sync_bgp_route_maps(
        nb,
        inventory,
        counters,
        prefix_lists=prefix_lists,
    )
    await _sync_bgp_peer_groups_and_sessions(nb, inventory, counters)


async def _sync_netbox_bgp_projection_if_enabled(
    nb: object,
    inventory: SdnInventory,
    counters: SdnSyncCounters,
    *,
    enabled: bool,
    available: bool | None,
) -> bool | None:
    if not enabled:
        return available
    if available is None:
        try:
            available = await _netbox_bgp_available(nb)
        except Exception as error:  # noqa: BLE001
            counters.record_endpoint_error(inventory.cluster_name)
            logger.warning("Could not probe netbox-bgp availability: %s", error)
            available = False
    if not available:
        counters.skipped += 1
        counters.record_object_warning(
            kind="bgp-projection",
            name=inventory.cluster_name,
            warning="Skipped because the optional netbox_bgp API is unavailable.",
        )
        return available
    try:
        await _sync_netbox_bgp_projection(nb, inventory, counters)
    except Exception as error:  # noqa: BLE001
        counters.record_endpoint_error(inventory.cluster_name)
        logger.warning(
            "Could not sync SDN BGP projection for %s: %s",
            inventory.cluster_name,
            error,
        )
    return available


async def _emit(
    bridge: WebSocketSSEBridge | None,
    status: str,
    message: str,
    *,
    result: dict[str, object] | None = None,
) -> None:
    if bridge is None:
        return
    payload: dict[str, object] = {
        "step": "sdn",
        "status": status,
        "message": message,
    }
    if result is not None:
        payload["result"] = result
    await bridge.emit("step", payload)


async def _resolve_plugin_endpoint_id(nb: object, px: object) -> int | None:
    configured_id = getattr(px, "db_endpoint_id", None)
    if configured_id is not None:
        existing = await rest_first_async(
            nb,
            "/api/plugins/proxbox/endpoints/proxmox/",
            query={"id": configured_id, "limit": 2},
        )
        if existing is not None:
            return int(configured_id)
    return await _get_netbox_endpoint_id(nb, px)


async def sync_sdn_to_netbox(
    *,
    netbox_session: object,
    pxs: list[object],
    websocket: WebSocketSSEBridge | None = None,
    use_websocket: bool = False,
    include_node_runtime: bool = True,
    sync_mode_sdn_bgp: str = "disabled",
) -> dict[str, object]:
    """Collect Proxmox SDN inventory and reconcile read-only NetBox state."""

    del use_websocket
    counters = SdnSyncCounters()
    sdn_bgp_enabled = _sdn_bgp_projection_enabled(sync_mode_sdn_bgp)
    netbox_bgp_available: bool | None = None

    for px in pxs:
        cluster_name = str(getattr(px, "cluster_name", None) or getattr(px, "name", None) or px)
        await _emit(websocket, "processing", f"Collecting SDN inventory for {cluster_name}.")
        try:
            endpoint_id = await _resolve_plugin_endpoint_id(netbox_session, px)
            inventory = await collect_sdn_inventory_for_session(
                px,
                endpoint_id=endpoint_id,
                include_node_runtime=include_node_runtime,
            )
        except Exception as error:  # noqa: BLE001
            counters.record_endpoint_error(cluster_name)
            logger.warning("Could not collect SDN inventory for %s: %s", cluster_name, error)
            continue

        counters.controllers += len(inventory.controllers)
        counters.zones += len(inventory.zones)
        counters.vnets += len(inventory.vnets)
        counters.skipped += len(inventory.skipped_warnings)
        if inventory.errors:
            for _ in inventory.errors:
                counters.record_endpoint_error(inventory.cluster_name)

        vnet_l2vpn_ids, subnet_prefix_ids = await _sync_netbox_l2vpn_objects(
            netbox_session,
            inventory,
            counters,
        )
        try:
            await _sync_l2vpn_terminations(
                netbox_session,
                inventory,
                counters,
                vnet_l2vpn_ids=vnet_l2vpn_ids,
            )
        except Exception as error:  # noqa: BLE001
            counters.record_endpoint_error(inventory.cluster_name)
            logger.warning("Could not sync SDN L2VPN terminations for %s: %s", cluster_name, error)
        try:
            await _sync_plugin_inventory(
                netbox_session,
                inventory,
                counters,
                vnet_l2vpn_ids=vnet_l2vpn_ids,
                subnet_prefix_ids=subnet_prefix_ids,
            )
            await _record_object_bindings(
                netbox_session,
                inventory,
                counters,
                vnet_l2vpn_ids=vnet_l2vpn_ids,
                subnet_prefix_ids=subnet_prefix_ids,
            )
        except Exception as error:  # noqa: BLE001
            counters.record_endpoint_error(inventory.cluster_name)
            logger.warning("Could not sync SDN plugin metadata for %s: %s", cluster_name, error)

        netbox_bgp_available = await _sync_netbox_bgp_projection_if_enabled(
            netbox_session,
            inventory,
            counters,
            enabled=sdn_bgp_enabled,
            available=netbox_bgp_available,
        )

        await _emit(
            websocket,
            "completed",
            f"Finished SDN sync for {cluster_name}.",
            result=counters.as_dict(),
        )

    return {"ok": True, "counters": counters.as_dict()}


__all__ = [
    "SdnControllerSchema",
    "SdnFabricSchema",
    "SdnInventory",
    "SdnNodeStatusSchema",
    "SdnPrefixListSchema",
    "SdnRouteMapSchema",
    "SdnSubnetSchema",
    "SdnSyncCounters",
    "SdnVNetSchema",
    "SdnZoneSchema",
    "_netbox_status",
    "_slug",
    "_split_csv",
    "_to_vnet",
    "_to_zone",
    "_valid_prefix",
    "collect_sdn_inventory",
    "collect_sdn_inventory_for_session",
    "sync_sdn_to_netbox",
]
