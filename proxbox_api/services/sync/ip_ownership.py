"""Ownership-safe IP address reconciliation helpers."""

from __future__ import annotations

from datetime import datetime

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxIpAddressSyncState


def _ip_address_current_normalizer(record: dict) -> dict:
    """Shared normalizer for ip-address reconcile drift comparison."""
    return {
        "address": record.get("address"),
        "assigned_object_type": record.get("assigned_object_type"),
        "assigned_object_id": record.get("assigned_object_id"),
        "status": record.get("status"),
        "dns_name": record.get("dns_name"),
        "tags": record.get("tags"),
    }


def _record_field(record: object, key: str, default: object = None) -> object:
    """Read a field from a NetBox record that may be a dict or a RestRecord."""
    if isinstance(record, dict):
        return record.get(key, default)
    serialize = getattr(record, "serialize", None)
    if callable(serialize):
        try:
            return serialize().get(key, default)
        except Exception:
            pass
    return getattr(record, key, default)


def _relation_id_or_none(value: object) -> int | None:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _host_address(ip_addr: str) -> str:
    """Return the host portion of a CIDR (drops the mask).

    NetBox's ``address`` filter is exact-CIDR, so querying ``1.2.3.4/24`` will
    NOT match an existing ``1.2.3.4/32`` record. Looking up adoptable records by
    host (mask-agnostic) lets us adopt a pre-existing address regardless of its
    stored mask — e.g. an operator-seeded ``/32`` — instead of failing to create
    a duplicate under ENFORCE_GLOBAL_UNIQUE.
    """
    return ip_addr.split("/", 1)[0]


async def _reconcile_interface_ip(
    nb,
    *,
    ip_addr: str,
    interface_id: int,
    tag_refs: list[dict],
    now: datetime,
    dns_name: str | None,
    interface_name: str,
    assigned_object_type: str = "virtualization.vminterface",
    interface_lookup_field: str = "vminterface_id",
) -> int | None:
    """Reconcile a single interface IP without stealing another object's address.

    Ownership is resolved before any write:

    * reuse a record already assigned to this interface,
    * adopt a record that is currently unassigned, or
    * create a new record scoped to this interface.

    A record assigned to a different object is never reused or reassigned.
    """
    interface_id = int(interface_id)
    payload = {
        "address": ip_addr,
        "assigned_object_type": assigned_object_type,
        "assigned_object_id": interface_id,
        "status": "active",
        "dns_name": dns_name or "",
        "tags": tag_refs,
        "custom_fields": {"proxmox_last_updated": now.isoformat()},
    }

    try:
        existing = await rest_list_async(
            nb,
            "/api/ipam/ip-addresses/",
            query={"address": _host_address(ip_addr), "limit": 50},
        )
    except Exception as list_exc:
        # If ownership cannot be resolved, fall back to the interface-scoped
        # create path below (which still never matches a foreign address)
        # rather than aborting the whole IP sync.
        logger.debug(
            "Ownership lookup for IP %s failed (%s); using interface-scoped create",
            ip_addr,
            list_exc,
        )
        existing = []

    own_record: object | None = None
    adoptable_record: object | None = None
    for record in existing or []:
        assigned_type = _record_field(record, "assigned_object_type")
        assigned_id = _relation_id_or_none(_record_field(record, "assigned_object_id"))
        if assigned_type == assigned_object_type and assigned_id == interface_id:
            own_record = record
            break
        if adoptable_record is None and assigned_type in (None, "") and assigned_id is None:
            adoptable_record = record

    target = own_record or adoptable_record
    try:
        record_id = (
            _relation_id_or_none(_record_field(target, "id")) if target is not None else None
        )
        if record_id is not None:
            # Reconcile the specific record we own/adopt by id. For an owned
            # record the assignment already matches; for an unassigned record
            # the assignment fields are patched, adopting it onto this interface.
            reconciled = await rest_reconcile_async(
                nb,
                "/api/ipam/ip-addresses/",
                lookup={"id": record_id},
                payload=payload,
                schema=NetBoxIpAddressSyncState,
                current_normalizer=_ip_address_current_normalizer,
                strict_lookup=True,
            )
        else:
            # No reusable record exists. Create one scoped to this interface; the
            # strict, interface-scoped lookup means the reconcile fallback can
            # never match a foreign address and reassign it.
            reconciled = await rest_reconcile_async(
                nb,
                "/api/ipam/ip-addresses/",
                lookup={"address": ip_addr, interface_lookup_field: interface_id},
                payload=payload,
                schema=NetBoxIpAddressSyncState,
                current_normalizer=_ip_address_current_normalizer,
                strict_lookup=True,
            )
    except Exception as ip_exc:
        logger.warning(
            "Failed to sync IP %s for interface %s: %s",
            ip_addr,
            interface_name,
            ip_exc,
        )
        return None

    return _relation_id_or_none(_record_field(reconciled, "id"))
