"""Virtual machine mapper utilities for Proxmox to NetBox conversions."""

from __future__ import annotations

from datetime import datetime

from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput, ProxmoxVmResourceInput
from proxbox_api.proxmox_to_netbox.normalize import build_virtual_machine_transform


def map_proxmox_vm_to_netbox_vm_body(
    resource: ProxmoxVmResourceInput | dict[str, object],
    config: ProxmoxVmConfigInput | dict[str, object] | None,
    *,
    cluster_id: int,
    device_id: int | None,
    role_id: int | None,
    tag_ids: list[int],
    site_id: int | None = None,
    tenant_id: int | None = None,
    virtual_machine_type_id: int | None = None,
    last_updated: datetime | None = None,
    cluster_name: str | None = None,
    proxmox_url: str | None = None,
) -> dict[str, object]:
    """Map Proxmox VM raw payload to NetBox VM create body dictionary."""

    body = build_virtual_machine_transform(
        resource=resource,
        config=config,
        cluster_id=cluster_id,
        device_id=device_id,
        site_id=site_id,
        tenant_id=tenant_id,
        role_id=role_id,
        tag_ids=tag_ids,
        virtual_machine_type_id=virtual_machine_type_id,
        last_updated=last_updated,
        cluster_name=cluster_name,
        proxmox_url=proxmox_url,
    )
    return body.model_dump(exclude_none=True, by_alias=True)
