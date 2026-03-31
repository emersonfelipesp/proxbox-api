"""Virtual machine synchronization service helpers for Proxmox-to-NetBox mapping."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from proxbox_api.proxmox_to_netbox.mappers.virtual_machine import (
    map_proxmox_vm_to_netbox_vm_body,
)


def build_netbox_virtual_machine_payload(
    *,
    proxmox_resource: dict[str, Any],
    proxmox_config: dict[str, Any] | None,
    cluster_id: int,
    device_id: int | None,
    role_id: int | None,
    tag_ids: list[int],
    last_updated: datetime | None = None,
) -> dict[str, Any]:
    """Build NetBox virtual machine payload from Proxmox raw resource/config payloads."""

    return map_proxmox_vm_to_netbox_vm_body(
        resource=proxmox_resource,
        config=proxmox_config,
        cluster_id=cluster_id,
        device_id=device_id,
        role_id=role_id,
        tag_ids=tag_ids,
        last_updated=last_updated,
    )
