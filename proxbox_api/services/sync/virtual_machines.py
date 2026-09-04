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
    platform_id: int | None = None,
    parse_description_metadata: bool = False,
    overwrite_flags: object | None = None,
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
        platform_id=platform_id,
        parse_description_metadata=parse_description_metadata,
        overwrite_flags=overwrite_flags,
    )


def build_virtual_machine_sync_state_fields(
    *,
    proxmox_resource: ProxmoxVmResourceInput | dict[str, Any],
    proxmox_config: ProxmoxVmConfigInput | dict[str, Any] | None,
    last_updated: datetime | None = None,
    cluster_name: str | None = None,
    proxmox_url: str | None = None,
    endpoint_id: int | None = None,
) -> dict[str, object]:
    """Build the live values persisted to the typed VM sync-state sidecar."""
    resource = (
        proxmox_resource
        if isinstance(proxmox_resource, ProxmoxVmResourceInput)
        else ProxmoxVmResourceInput.model_validate(proxmox_resource)
    )
    config = (
        proxmox_config
        if isinstance(proxmox_config, ProxmoxVmConfigInput)
        else ProxmoxVmConfigInput.model_validate(proxmox_config or {})
    )
    vm_type = resource.type if resource.type in {"qemu", "lxc"} else "unknown"
    fields: dict[str, object] = {
        "proxmox_vm_id": resource.vmid,
        "proxmox_vm_type": vm_type,
        "proxmox_start_at_boot": config.start_at_boot,
        "proxmox_unprivileged_container": config.unprivileged_container,
        "proxmox_qemu_agent": config.qemu_agent_enabled,
        "proxmox_search_domain": config.searchdomain,
        "proxmox_node": resource.node,
        "proxmox_status": resource.status,
        "proxmox_endpoint_id": endpoint_id,
    }
    if cluster_name:
        fields["proxmox_cluster"] = cluster_name
    if proxmox_url:
        fields["proxmox_link"] = f"{proxmox_url}/#v1:0:={vm_type}/{resource.vmid}"
    if last_updated:
        fields["proxmox_last_updated"] = last_updated.isoformat()
    return fields
