"""VM creation and dependency initialization - extracted from sync_vm.py."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxDeviceRoleSyncState,
    NetBoxVirtualMachineCreateBody,
    ProxmoxVmConfigInput,
    ProxmoxVmResourceInput,
)
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep
from proxbox_api.services.sync.devices import (
    _ensure_cluster,
    _ensure_cluster_type,
    _ensure_device,
    _ensure_device_type,
    _ensure_manufacturer,
    _ensure_site,
)
from proxbox_api.services.sync.devices import (
    _ensure_device_role as _ensure_proxmox_node_role,
)
from proxbox_api.services.sync.virtual_machines import build_netbox_virtual_machine_payload

# VM role mappings for different VM types
VM_ROLE_MAPPINGS = {
    "qemu": {
        "name": "Virtual Machine (QEMU)",
        "slug": "virtual-machine-qemu",
        "color": "00ffff",
        "description": "Proxmox Virtual Machine",
        "vm_role": True,
    },
    "lxc": {
        "name": "Container (LXC)",
        "slug": "container-lxc",
        "color": "7fffd4",
        "description": "Proxmox LXC Container",
        "vm_role": True,
    },
    "undefined": {
        "name": "Unknown",
        "slug": "unknown",
        "color": "000000",
        "description": "VM Type not found. Neither QEMU nor LXC.",
        "vm_role": True,
    },
}


async def ensure_vm_dependencies(
    netbox_session: NetBoxSessionDep,
    cluster_status: ClusterStatusDep,
    cluster_name: str,
    tag_id: int,
    tag_refs: list[dict],
    node_name: str | None = None,
) -> tuple:
    """Ensure all VM dependencies exist in NetBox (cluster, device, roles, site).

    Args:
        netbox_session: NetBox session
        cluster_status: Cluster status from Proxmox
        cluster_name: Name of cluster
        tag_id: ID of sync tag
        tag_refs: Tag references
        node_name: Optional Proxmox node name

    Returns:
        Tuple of (cluster, device) NetBox objects

    Raises:
        ProxboxException: If dependency creation fails
    """
    try:
        cluster_mode = next(
            (
                cluster_state.mode
                for cluster_state in cluster_status
                if getattr(cluster_state, "name", None) == cluster_name
            ),
            "cluster",
        )

        cluster_type = await _ensure_cluster_type(
            netbox_session,
            mode=cluster_mode,
            tag_refs=tag_refs,
        )
        cluster = await _ensure_cluster(
            netbox_session,
            cluster_name=cluster_name,
            cluster_type_id=getattr(cluster_type, "id", None),
            mode=cluster_mode,
            tag_refs=tag_refs,
        )
        manufacturer = await _ensure_manufacturer(
            netbox_session,
            tag_refs=tag_refs,
        )
        device_type = await _ensure_device_type(
            netbox_session,
            manufacturer_id=getattr(manufacturer, "id", None),
            tag_refs=tag_refs,
        )
        device_role = await _ensure_proxmox_node_role(
            netbox_session,
            tag_refs=tag_refs,
        )
        site = await _ensure_site(
            netbox_session,
            cluster_name=cluster_name,
            tag_refs=tag_refs,
        )
        device = await _ensure_device(
            netbox_session,
            device_name=node_name or cluster_name,
            cluster_id=getattr(cluster, "id", None),
            device_type_id=getattr(device_type, "id", None),
            role_id=getattr(device_role, "id", None),
            site_id=getattr(site, "id", None),
            tag_refs=tag_refs,
        )

        logger.debug("VM dependencies ready: cluster=%s, device=%s", cluster, device)
        return cluster, device

    except Exception as error:
        raise ProxboxException(
            message="Error creating VM dependent objects (cluster, device, tag, role)",
            python_exception=str(error),
        )


async def ensure_vm_role(
    netbox_session: NetBoxSessionDep,
    vm_type: str,
    tag_id: int,
    tag_refs: list[dict],
) -> dict:
    """Ensure the VM role (e.g., "Virtual Machine (QEMU)") exists in NetBox.

    Args:
        netbox_session: NetBox session
        vm_type: VM type ("qemu", "lxc", or "undefined")
        tag_id: ID of sync tag
        tag_refs: Tag references

    Returns:
        NetBox device role dict
    """
    role_mapping = VM_ROLE_MAPPINGS.get(vm_type, VM_ROLE_MAPPINGS["undefined"])

    return await rest_reconcile_async(
        netbox_session,
        "/api/dcim/device-roles/",
        lookup={"slug": role_mapping.get("slug")},
        payload={
            **role_mapping,
            "tags": tag_refs,
        },
        schema=NetBoxDeviceRoleSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "color": record.get("color"),
            "description": record.get("description"),
            "vm_role": record.get("vm_role"),
            "tags": record.get("tags"),
        },
    )


async def create_or_update_virtual_machine(
    netbox_session: NetBoxSessionDep,
    proxmox_resource: ProxmoxVmResourceInput | dict[str, object],
    proxmox_config: ProxmoxVmConfigInput | dict[str, object] | None,
    cluster_id: int,
    device_id: int,
    role_id: int,
    tag_id: int,
    tag_refs: list[dict[str, object]],
) -> dict:
    """Create or update a virtual machine in NetBox.

    Args:
        netbox_session: NetBox session
        proxmox_resource: Proxmox resource dict
        proxmox_config: Proxmox config dict (optional)
        cluster_id: NetBox cluster ID
        device_id: NetBox device ID
        role_id: NetBox role ID
        tag_id: NetBox tag ID
        tag_refs: Tag references

    Returns:
        NetBox virtual machine dict

    Raises:
        ProxboxException: If VM creation fails
    """
    now = datetime.now(timezone.utc)

    payload = build_netbox_virtual_machine_payload(
        proxmox_resource=proxmox_resource,
        proxmox_config=proxmox_config,
        cluster_id=cluster_id,
        device_id=device_id,
        role_id=role_id,
        tag_ids=[tag_id],
        last_updated=now,
    )

    virtual_machine = await rest_reconcile_async(
        netbox_session,
        "/api/virtualization/virtual-machines/",
        lookup={
            "cf_proxmox_vm_id": int(proxmox_resource.get("vmid")),
            "cluster_id": cluster_id,
        },
        payload=payload,
        schema=NetBoxVirtualMachineCreateBody,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "status": record.get("status"),
            "cluster": record.get("cluster"),
            "device": record.get("device"),
            "role": record.get("role"),
            "vcpus": record.get("vcpus"),
            "memory": record.get("memory"),
            "disk": record.get("disk"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
            "description": record.get("description"),
        },
    )

    logger.debug("Created/updated virtual machine: %s", virtual_machine)
    return virtual_machine if isinstance(virtual_machine, dict) else virtual_machine.dict()
