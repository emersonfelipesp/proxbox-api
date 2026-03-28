"""Virtual machine mapper utilities for Proxmox to NetBox conversions."""

from __future__ import annotations

from typing import Any

from proxbox_api.proxmox_to_netbox.normalize import build_virtual_machine_transform


def map_proxmox_vm_to_netbox_vm_body(
    resource: dict[str, Any],
    config: dict[str, Any] | None,
    *,
    cluster_id: int,
    device_id: int | None,
    role_id: int | None,
    tag_ids: list[int],
) -> dict[str, Any]:
    """Map Proxmox VM raw payload to NetBox VM create body dictionary."""

    body = build_virtual_machine_transform(
        resource=resource,
        config=config,
        cluster_id=cluster_id,
        device_id=device_id,
        role_id=role_id,
        tag_ids=tag_ids,
    )
    return body.model_dump(exclude_none=True, by_alias=True)
