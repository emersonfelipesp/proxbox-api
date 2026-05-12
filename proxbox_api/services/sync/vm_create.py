"""VM creation and dependency initialization - extracted from sync_vm.py."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.constants import VM_ROLE_MAPPINGS, VM_TYPE_MAPPINGS
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.netbox_version import detect_netbox_version, supports_virtual_machine_type
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxDeviceRoleSyncState,
    NetBoxVirtualMachineCreateBody,
    NetBoxVirtualMachineTypeSyncState,
    ProxmoxVmConfigInput,
    ProxmoxVmResourceInput,
)
from proxbox_api.schemas.proxmox import ClusterStatusSchemaList
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.devices import (
    _ensure_cluster,
    _ensure_cluster_type,
    _ensure_device,
    _ensure_device_type,
    _ensure_manufacturer,
    _ensure_site,
    _resolve_tenant,
)
from proxbox_api.services.sync.devices import (
    _ensure_device_role as _ensure_proxmox_node_role,
)
from proxbox_api.services.sync.virtual_machines import build_netbox_virtual_machine_payload
from proxbox_api.services.sync.vm_helpers import (
    _compute_vm_patchable_fields,
    normalize_current_virtual_machine_payload,
)


async def ensure_vm_dependencies(
    netbox_session: object,
    cluster_status: ClusterStatusSchemaList,
    cluster_name: str,
    tag_id: int,
    tag_refs: list[dict],
    node_name: str | None = None,
    *,
    overwrite_flags: SyncOverwriteFlags | None = None,
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
        cluster_state = next(
            (state for state in cluster_status if getattr(state, "name", None) == cluster_name),
            None,
        )
        cluster_mode = getattr(cluster_state, "mode", None) or "cluster"

        cluster_type = await _ensure_cluster_type(
            netbox_session,
            mode=cluster_mode,
            tag_refs=tag_refs,
        )
        site = await _ensure_site(
            netbox_session,
            cluster_name=cluster_name,
            tag_refs=tag_refs,
            placement=cluster_state,
        )
        tenant = await _resolve_tenant(netbox_session, placement=cluster_state)
        cluster = await _ensure_cluster(
            netbox_session,
            cluster_name=cluster_name,
            cluster_type_id=getattr(cluster_type, "id", None),
            mode=cluster_mode,
            tag_refs=tag_refs,
            site_id=getattr(site, "id", None),
            tenant_id=getattr(tenant, "id", None),
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
        device = await _ensure_device(
            netbox_session,
            device_name=node_name or cluster_name,
            cluster_id=getattr(cluster, "id", None),
            device_type_id=getattr(device_type, "id", None),
            role_id=getattr(device_role, "id", None),
            site_id=getattr(site, "id", None),
            tag_refs=tag_refs,
            overwrite_device_role=(
                overwrite_flags.overwrite_device_role if overwrite_flags else True
            ),
            overwrite_device_type=(
                overwrite_flags.overwrite_device_type if overwrite_flags else True
            ),
            overwrite_device_tags=(
                overwrite_flags.overwrite_device_tags if overwrite_flags else True
            ),
            overwrite_flags=overwrite_flags,
        )

        logger.debug("VM dependencies ready: cluster=%s, device=%s", cluster, device)
        return cluster, device

    except Exception as error:
        raise ProxboxException(
            message="Error creating VM dependent objects (cluster, device, tag, role)",
            python_exception=str(error),
        )


async def ensure_vm_role(
    netbox_session: object,
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


async def ensure_vm_type(
    netbox_session: object,
    vm_type: str,
    tag_refs: list[dict],
) -> object | None:
    """Ensure a NetBox VirtualMachineType object exists for the given Proxmox VM type (NetBox v4.6+).

    Args:
        netbox_session: NetBox session
        vm_type: Proxmox VM type ("qemu" or "lxc")
        tag_refs: Tag references

    Returns:
        NetBox VirtualMachineType object, or None if vm_type is not recognised.
    """
    type_data = VM_TYPE_MAPPINGS.get(vm_type)
    if not type_data:
        return None

    netbox_version = await detect_netbox_version(netbox_session)
    if not supports_virtual_machine_type(netbox_version):
        logger.debug(
            "Skipping NetBox VirtualMachineType sync for vm_type=%s on NetBox version %s",
            vm_type,
            ".".join(str(part) for part in netbox_version),
        )
        return None

    return await rest_reconcile_async(
        netbox_session,
        "/api/virtualization/virtual-machine-types/",
        lookup={"slug": type_data["slug"]},
        payload={
            **type_data,
            "tags": tag_refs,
        },
        schema=NetBoxVirtualMachineTypeSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "description": record.get("description"),
            "tags": record.get("tags"),
        },
    )


async def create_or_update_virtual_machine(
    netbox_session: object,
    proxmox_resource: ProxmoxVmResourceInput | dict[str, object],
    proxmox_config: ProxmoxVmConfigInput | dict[str, object] | None,
    cluster_id: int,
    device_id: int,
    role_id: int | None,
    tag_id: int,
    tag_refs: list[dict[str, object]],
    cluster_name: str | None = None,
    virtual_machine_type_id: int | None = None,
    site_id: int | None = None,
    tenant_id: int | None = None,
    overwrite_flags: SyncOverwriteFlags | None = None,
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
        cluster_name: Proxmox cluster name for custom field population.
        virtual_machine_type_id: Optional NetBox VirtualMachineType ID (NetBox v4.6+).
        overwrite_flags: Per-field overwrite gates for existing VM updates.

    Returns:
        NetBox virtual machine dict

    Raises:
        ProxboxException: If VM creation fails
    """
    now = datetime.now(timezone.utc)

    raw_vmid = (
        proxmox_resource.get("vmid")
        if isinstance(proxmox_resource, dict)
        else getattr(proxmox_resource, "vmid", None)
    )
    if raw_vmid is None or (isinstance(raw_vmid, str) and not raw_vmid.strip()):
        raise ProxboxException(
            message="Proxmox resource is missing 'vmid'; cannot reconcile VM in NetBox.",
            detail=f"resource keys: {sorted(proxmox_resource.keys()) if isinstance(proxmox_resource, dict) else type(proxmox_resource).__name__}",
        )
    try:
        vmid_int = int(raw_vmid)
    except (TypeError, ValueError) as exc:
        raise ProxboxException(
            message="Proxmox resource has a non-integer 'vmid'.",
            python_exception=str(exc),
        )

    netbox_version = await detect_netbox_version(netbox_session)
    supports_vm_type = supports_virtual_machine_type(netbox_version)
    resolved_virtual_machine_type_id = virtual_machine_type_id if supports_vm_type else None

    payload = build_netbox_virtual_machine_payload(
        proxmox_resource=proxmox_resource,
        proxmox_config=proxmox_config,
        cluster_id=cluster_id,
        device_id=device_id,
        role_id=None if resolved_virtual_machine_type_id is not None else role_id,
        tag_ids=[tag_id],
        site_id=site_id,
        tenant_id=tenant_id,
        virtual_machine_type_id=resolved_virtual_machine_type_id,
        last_updated=now,
        cluster_name=cluster_name,
    )

    virtual_machine = await rest_reconcile_async(
        netbox_session,
        "/api/virtualization/virtual-machines/",
        lookup={
            "cf_proxmox_vm_id": vmid_int,
            "cluster_id": cluster_id,
        },
        payload=payload,
        schema=NetBoxVirtualMachineCreateBody,
        patchable_fields=frozenset(
            _compute_vm_patchable_fields(
                overwrite_flags,
                supports_virtual_machine_type_field=supports_vm_type,
            )
        ),
        current_normalizer=lambda record: normalize_current_virtual_machine_payload(
            record,
            supports_virtual_machine_type_field=supports_vm_type,
        ),
    )

    logger.debug("Created/updated virtual machine: %s", virtual_machine)
    return virtual_machine if isinstance(virtual_machine, dict) else virtual_machine.dict()
