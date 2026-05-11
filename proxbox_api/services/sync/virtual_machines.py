"""Virtual machine synchronization service helpers for Proxmox-to-NetBox mapping."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from proxbox_api.proxmox_to_netbox.mappers.virtual_machine import (
    map_proxmox_vm_to_netbox_vm_body,
)
from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput, ProxmoxVmResourceInput
from proxbox_api.types import VMPayloadDict


def build_netbox_virtual_machine_payload(
    *,
    proxmox_resource: ProxmoxVmResourceInput | dict[str, Any],
    proxmox_config: ProxmoxVmConfigInput | dict[str, Any] | None,
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
) -> VMPayloadDict:
    """Build NetBox virtual machine payload from Proxmox raw resource/config payloads.

    Args:
        proxmox_resource: Proxmox VM resource data as model or dict.
        proxmox_config: Proxmox VM config data as model or dict.
        cluster_id: NetBox cluster ID.
        device_id: Optional NetBox device ID for physical host.
        site_id: Optional NetBox site ID for VM placement.
        tenant_id: Optional NetBox tenant ID for VM placement.
        role_id: Optional NetBox VM role ID.
        tag_ids: List of NetBox tag IDs to apply.
        virtual_machine_type_id: Optional NetBox VirtualMachineType ID (NetBox v4.6+).
        last_updated: Optional timestamp for last update.
        cluster_name: Proxmox cluster name for custom field population.
        proxmox_url: Proxmox base URL for link custom field population.

    Returns:
        VMPayloadDict with structure for NetBox VM creation/update.
    """

    return map_proxmox_vm_to_netbox_vm_body(
        resource=proxmox_resource,
        config=proxmox_config,
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
