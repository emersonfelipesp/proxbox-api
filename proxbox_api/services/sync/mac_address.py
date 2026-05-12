"""MAC address reconciliation for VM and node interfaces.

NetBox 4.2 introduced `dcim.MACAddress` as a first-class model. NetBox 4.5/4.6
ship the legacy inline `mac_address` field on `VMInterface` / `Interface` as
**read-only** (computed from `primary_mac_address`). Writes to it are silently
dropped — historically `proxbox-api` sent MACs there and they never appeared
in NetBox.

This module owns the new write path:

1. Upsert a `dcim.MACAddress` row attached to the interface via the GFK
   (`assigned_object_type` / `assigned_object_id`).
2. PATCH the interface's `primary_mac_address` FK to point at the row, only
   when it currently points elsewhere.

The MAC upsert uses `rest_reconcile_async` so a second consecutive sync with
unchanged data emits zero `ObjectChange` rows for MAC records — the acceptance
criterion from issue
https://github.com/emersonfelipesp/netbox-proxbox/issues/359.

This helper is intentionally narrow; the `createOrUpdate` work in
https://github.com/emersonfelipesp/netbox-proxbox/issues/357 can absorb it
later as one case of a generic typed writer module.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    rest_first_async,
    rest_patch_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import NetBoxMACAddressSyncState

MacReconcileStatus = Literal["unchanged", "created", "updated", "skipped"]

VMINTERFACE_CONTENT_TYPE = "virtualization.vminterface"
DCIM_INTERFACE_CONTENT_TYPE = "dcim.interface"


def normalize_mac(value: object) -> str | None:
    """Return NetBox canonical MAC form (uppercase, colon-separated) or None.

    Accepts colon- or hyphen-separated input. Empty/None returns None so
    callers can skip the upsert when the source has no MAC.
    """
    if value in (None, ""):
        return None
    text = str(value).strip().replace("-", ":").upper()
    return text or None


async def reconcile_mac_for_interface(
    nb: object,
    *,
    mac: str | None,
    assigned_object_type: str,
    assigned_object_id: int,
    interface_list_path: str,
    tag_refs: list[dict] | None = None,
) -> tuple[int | None, MacReconcileStatus]:
    """Ensure a `dcim.MACAddress` exists and is the primary MAC on the interface.

    Args:
        nb: NetBox client/session.
        mac: Source MAC (any case, colons or hyphens). When None/empty the
            function is a no-op and returns ``(None, "skipped")``.
        assigned_object_type: Lowercase-dotted content type for the interface
            (`virtualization.vminterface` or `dcim.interface`).
        assigned_object_id: NetBox interface ID.
        interface_list_path: REST list path for the interface model, used to
            PATCH ``primary_mac_address`` (e.g. ``/api/virtualization/interfaces/``).
        tag_refs: Optional Proxbox tag references propagated onto the MAC row.

    Returns:
        ``(macaddress_id, status)`` where ``status`` is one of
        ``"created" | "updated" | "unchanged" | "skipped"``. ``status`` reports
        the MAC-row outcome; the interface PATCH is folded into "updated" when
        the FK was flipped on this call.
    """
    canonical = normalize_mac(mac)
    if canonical is None:
        return None, "skipped"
    if not assigned_object_id:
        # Defensive: caller passed an interface that wasn't created.
        logger.debug(
            "reconcile_mac_for_interface called without assigned_object_id (mac=%s)",
            canonical,
        )
        return None, "skipped"

    payload: dict[str, object] = {
        "mac_address": canonical,
        "assigned_object_type": assigned_object_type,
        "assigned_object_id": int(assigned_object_id),
        "tags": tag_refs or [],
    }

    mac_record = await rest_reconcile_async(
        nb,
        "/api/dcim/mac-addresses/",
        lookup={
            "mac_address": canonical,
            "assigned_object_type": assigned_object_type,
            "assigned_object_id": int(assigned_object_id),
        },
        payload=payload,
        schema=NetBoxMACAddressSyncState,
        current_normalizer=lambda record: {
            "mac_address": normalize_mac(record.get("mac_address")),
            "assigned_object_type": _content_type_value(record.get("assigned_object_type")),
            "assigned_object_id": _relation_id(record.get("assigned_object_id"))
            or _relation_id(record.get("assigned_object")),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
        strict_lookup=True,
    )
    mac_id = (
        mac_record.get("id")
        if isinstance(mac_record, dict)
        else getattr(mac_record, "id", None)
    )
    if not isinstance(mac_id, int):
        return None, "skipped"

    # Ensure the interface's primary_mac_address FK points at this row, but
    # only PATCH on diff so a stable sync stays silent.
    current_iface = await rest_first_async(
        nb,
        interface_list_path,
        query={"id": int(assigned_object_id), "limit": 1},
    )
    current_primary = _relation_id(_get(current_iface, "primary_mac_address"))
    if current_primary == mac_id:
        return mac_id, "unchanged"

    await rest_patch_async(
        nb,
        interface_list_path,
        int(assigned_object_id),
        {"primary_mac_address": mac_id},
    )
    return mac_id, "updated"


async def reconcile_mac_for_vm_interface(
    nb: object,
    *,
    vminterface_id: int,
    mac: str | None,
    tag_refs: list[dict] | None = None,
) -> tuple[int | None, MacReconcileStatus]:
    """Convenience wrapper for VMInterface (virtualization.vminterface)."""
    return await reconcile_mac_for_interface(
        nb,
        mac=mac,
        assigned_object_type=VMINTERFACE_CONTENT_TYPE,
        assigned_object_id=vminterface_id,
        interface_list_path="/api/virtualization/interfaces/",
        tag_refs=tag_refs,
    )


def _relation_id(value: object) -> object:
    if isinstance(value, dict):
        return cast(dict[str, Any], value).get("id")
    return value


def _content_type_value(value: object) -> object:
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        app_label = mapping.get("app_label") or mapping.get("app")
        model = mapping.get("model")
        if app_label and model:
            return f"{app_label}.{model}"
        return mapping.get("value") or mapping.get("label")
    return value


def _get(record: object, field: str) -> object:
    if record is None:
        return None
    if isinstance(record, dict):
        return cast(dict[str, Any], record).get(field)
    return getattr(record, field, None)
