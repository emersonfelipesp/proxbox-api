"""Cloud customer-network settings and NetBox IP allocation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address, ip_interface

from proxbox_api import runtime_settings
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_bulk_delete_async, rest_create_async, rest_list_async

_AVAILABLE_IPS_PATH = "/api/ipam/prefixes/{prefix_id}/available-ips/"
_IP_ADDRESSES_PATH = "/api/ipam/ip-addresses/"
_VMINTERFACE_CONTENT_TYPE = "virtualization.vminterface"


@dataclass(frozen=True, slots=True)
class CloudNetworkConfig:
    lock_enabled: bool
    prefix_id: int | None
    bridge: str
    vlan_tag: int | None
    gateway: str


@dataclass(frozen=True, slots=True)
class AvailableIPAddress:
    address: str


@dataclass(frozen=True, slots=True)
class AllocatedIPAddress:
    id: int | None
    address: str
    cidr: str


def resolve_cloud_network() -> CloudNetworkConfig:
    """Resolve the managed cloud customer network from runtime settings."""
    prefix_id = runtime_settings.get_int(
        settings_key="cloud_customer_prefix_id",
        env="PROXBOX_CLOUD_CUSTOMER_PREFIX_ID",
        default=0,
        minimum=0,
    )
    vlan_tag = runtime_settings.get_int(
        settings_key="cloud_customer_vlan_tag",
        env="PROXBOX_CLOUD_CUSTOMER_VLAN_TAG",
        default=0,
        minimum=0,
        maximum=4094,
    )
    return CloudNetworkConfig(
        lock_enabled=runtime_settings.get_bool(
            settings_key="cloud_network_lock_enabled",
            env="PROXBOX_CLOUD_NETWORK_LOCK_ENABLED",
            default=False,
        ),
        prefix_id=prefix_id if prefix_id > 0 else None,
        bridge=runtime_settings.get_str(
            settings_key="cloud_customer_bridge",
            env="PROXBOX_CLOUD_CUSTOMER_BRIDGE",
            default="",
        ),
        vlan_tag=vlan_tag if vlan_tag > 0 else None,
        gateway=runtime_settings.get_str(
            settings_key="cloud_customer_gateway",
            env="PROXBOX_CLOUD_CUSTOMER_GATEWAY",
            default="",
        ),
    )


def validate_cloud_network_configured(config: CloudNetworkConfig) -> None:
    """Raise ValueError when the managed cloud network is not usable."""
    if config.prefix_id is None or config.prefix_id < 1:
        raise ValueError("cloud network not configured")
    if not config.bridge.strip() or not config.gateway.strip():
        raise ValueError("cloud network not configured")
    try:
        ip_address(config.gateway)
    except ValueError as exc:
        raise ValueError("cloud network not configured") from exc


def _available_ips_path(prefix_id: int) -> str:
    return _AVAILABLE_IPS_PATH.format(prefix_id=prefix_id)


def _record_value(record: object, key: str) -> object:
    if isinstance(record, dict):
        return record.get(key)
    getter = getattr(record, "get", None)
    if callable(getter):
        return getter(key)
    return getattr(record, key, None)


def _normalize_allocated_address(record: object) -> AllocatedIPAddress:
    raw_id = _record_value(record, "id")
    ip_id: int | None
    try:
        ip_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        ip_id = None

    raw_address = str(_record_value(record, "address") or "").strip()
    if not raw_address:
        raise ValueError("NetBox allocation response did not include an address")

    try:
        parsed = ip_interface(raw_address)
        return AllocatedIPAddress(
            id=ip_id,
            address=str(parsed.ip),
            cidr=f"{parsed.ip}/{parsed.network.prefixlen}",
        )
    except ValueError:
        return AllocatedIPAddress(id=ip_id, address=raw_address.split("/", 1)[0], cidr=raw_address)


def _normalize_available_address(record: object) -> AvailableIPAddress | None:
    raw_address = str(_record_value(record, "address") or "").strip()
    if not raw_address:
        return None
    return AvailableIPAddress(address=raw_address)


async def _default_netbox_session() -> object:
    from proxbox_api.app.netbox_session import get_raw_netbox_session

    return get_raw_netbox_session()


async def peek_available_ips(
    prefix_id: int,
    limit: int,
    *,
    netbox_session: object | None = None,
) -> list[AvailableIPAddress]:
    """List available IPs from a NetBox prefix without occupying them."""
    nb = netbox_session if netbox_session is not None else await _default_netbox_session()
    records = await rest_list_async(
        nb,
        _available_ips_path(prefix_id),
        query={"limit": limit},
    )
    available: list[AvailableIPAddress] = []
    for record in records:
        item = _normalize_available_address(record)
        if item is not None:
            available.append(item)
    return available


async def allocate_ip(
    prefix_id: int,
    *,
    vminterface_id: int | None = None,
    status: str = "active",
    netbox_session: object | None = None,
) -> AllocatedIPAddress:
    """Atomically allocate and occupy the next available IP from a NetBox prefix."""
    nb = netbox_session if netbox_session is not None else await _default_netbox_session()
    payload: dict[str, object] = {"status": status}
    if vminterface_id is not None:
        payload.update(
            {
                "assigned_object_type": _VMINTERFACE_CONTENT_TYPE,
                "assigned_object_id": vminterface_id,
            }
        )

    record = await rest_create_async(nb, _available_ips_path(prefix_id), payload)
    return _normalize_allocated_address(record)


async def release_ip(
    ip_id: int,
    *,
    netbox_session: object | None = None,
) -> bool:
    """Best-effort rollback for an allocated NetBox IPAddress."""
    try:
        nb = netbox_session if netbox_session is not None else await _default_netbox_session()
        deleted = await rest_bulk_delete_async(nb, _IP_ADDRESSES_PATH, [ip_id])
        return deleted > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("cloud network: failed to release allocated IP id=%s: %s", ip_id, exc)
        return False


__all__ = [
    "AllocatedIPAddress",
    "AvailableIPAddress",
    "CloudNetworkConfig",
    "allocate_ip",
    "peek_available_ips",
    "release_ip",
    "resolve_cloud_network",
    "validate_cloud_network_configured",
]
