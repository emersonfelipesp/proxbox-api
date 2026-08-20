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
from proxbox_api.services.custom_fields import (
    legacy_custom_field_fallback_query,
    legacy_custom_fields_payload,
)
from proxbox_api.services.sync.cloudinit import sync_vm_cloudinit
from proxbox_api.services.sync.devices import (
    _effective_cluster_site_id,
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
from proxbox_api.services.sync.role_resolution import (
    apply_role_snapshot_policy,
    persist_sync_state_with_role_compensation,
    resolve_snapshot_read,
)
from proxbox_api.services.sync.sync_state_reader import resolve_virtual_machine_by_sync_state
from proxbox_api.services.sync.sync_state_writer import write_virtual_machine_sync_state
from proxbox_api.services.sync.virtual_machines import build_netbox_virtual_machine_payload
from proxbox_api.services.sync.vm_helpers import (
    _compute_vm_patchable_fields,
    normalize_current_virtual_machine_payload,
    relation_id,
    to_mapping,
)
from proxbox_api.services.sync.vmid_helpers import normalize_positive_int


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
            overwrite_flags=overwrite_flags,
        )
        site_id = _effective_cluster_site_id(
            cluster,
            fallback_site_id=getattr(site, "id", None),
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
            site_id=site_id,
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
    *,
    netbox_version: tuple[int, ...] | None = None,
) -> object | None:
    """Ensure a NetBox VirtualMachineType object exists for the given Proxmox VM type (NetBox v4.6+).

    Args:
        netbox_session: NetBox session
        vm_type: Proxmox VM type ("qemu" or "lxc")
        tag_refs: Tag references
        netbox_version: Pre-resolved version tuple; detected once if omitted.

    Returns:
        NetBox VirtualMachineType object, or None if vm_type is not recognised.
    """
    type_data = VM_TYPE_MAPPINGS.get(vm_type)
    if not type_data:
        return None

    if netbox_version is None:
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


async def ensure_vm_platform(
    netbox_session: object,
    *,
    ostype: object,
    guest_agent_osinfo: object = None,
    tag_refs: list[dict[str, object]] | None = None,
    cache: dict[str, int | None] | None = None,
) -> int | None:
    """Resolve (creating if needed) the NetBox platform for a guest OS.

    Returns the platform id, or ``None`` when the guest OS could not be identified --
    in which case the VM's platform is left unset rather than guessed at.

    ``cache`` is a run-scoped ``{platform name: id or None}`` map. Without it this does a
    NetBox reconcile **per virtual machine**, and an estate's VMs overwhelmingly share a
    handful of operating systems -- 500 VMs would mean 500 lookups for perhaps five
    platforms. Negative results are cached too, so an unmapped ``ostype`` is not retried
    once per VM either. Callers that sync a single VM can omit it.

    Never raises. A platform is a nice-to-have inventory detail; failing a VM's sync
    because NetBox would not accept a platform record would trade a blank field for a
    broken sync.
    """
    from proxbox_api.proxmox_to_netbox.guest_os import platform_slug, resolve_platform_name
    from proxbox_api.services.netbox_writers import upsert_platform

    name = resolve_platform_name(ostype=ostype, guest_agent_osinfo=guest_agent_osinfo)
    if not name:
        return None

    if cache is not None and name in cache:
        return cache[name]

    slug = platform_slug(name)
    if not slug:
        if cache is not None:
            cache[name] = None
        return None

    try:
        result = await upsert_platform(
            netbox_session,
            name=name,
            slug=slug,
            tag_refs=tag_refs or [],
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not reconcile NetBox platform %r: %s", name, error)
        # Deliberately not cached: a transient NetBox failure should not blank the
        # platform for every remaining VM in the run.
        return None

    record = getattr(result, "record", None)
    serialized = record.serialize() if record is not None else None
    platform_id = serialized.get("id") if isinstance(serialized, dict) else None
    try:
        resolved = int(platform_id) or None
    except (TypeError, ValueError):
        resolved = None

    if cache is not None:
        # A benign race is possible when VMs are prepared concurrently: two coroutines
        # can both miss and both reconcile. The reconcile is idempotent and matched by
        # slug, so the cost is one redundant lookup, never a duplicate record.
        cache[name] = resolved
    return resolved


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
    endpoint_id: int | None = None,
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
    endpoint_lookup_id = normalize_positive_int(endpoint_id)

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
        endpoint_id=endpoint_id,
    )

    vm_lookup = {
        key: value
        for key, value in {
            "cf_proxmox_vm_id": vmid_int,
            "cf_proxmox_endpoint_id": endpoint_lookup_id,
            "cluster_id": cluster_id if endpoint_lookup_id is None else None,
        }.items()
        if value is not None
    }
    existing_resolution = await resolve_virtual_machine_by_sync_state(
        netbox_session,
        proxmox_vm_id=vmid_int,
        endpoint_id=endpoint_lookup_id,
        cluster_id=cluster_id,
        fallback_query=legacy_custom_field_fallback_query(vm_lookup),
        fail_on_ambiguous=True,
    )

    existing_record = (
        to_mapping(existing_resolution.record) if existing_resolution is not None else None
    )
    snapshot_read = (
        await resolve_snapshot_read(netbox_session, existing_record)
        if existing_record is not None
        else None
    )
    payload, patchable_fields, role_decision = apply_role_snapshot_policy(
        existing_record=existing_record,
        existing_snapshot_id=(snapshot_read.snapshot_id if snapshot_read is not None else None),
        desired_payload=payload,
        patchable_fields=_compute_vm_patchable_fields(
            overwrite_flags,
            supports_virtual_machine_type_field=supports_vm_type,
        ),
        overwrite_vm_role=(
            overwrite_flags.overwrite_vm_role if overwrite_flags is not None else True
        ),
        snapshot_read_verified=(snapshot_read.verified if snapshot_read is not None else True),
    )

    virtual_machine = await rest_reconcile_async(
        netbox_session,
        "/api/virtualization/virtual-machines/",
        lookup=vm_lookup,
        payload=legacy_custom_fields_payload(
            payload,
            overwrite=(overwrite_flags is None or overwrite_flags.overwrite_vm_custom_fields),
            context="legacy VM custom-field payload",
        ),
        schema=NetBoxVirtualMachineCreateBody,
        patchable_fields=patchable_fields,
        current_normalizer=lambda record: normalize_current_virtual_machine_payload(
            record,
            supports_virtual_machine_type_field=supports_vm_type,
        ),
        strict_lookup=True,
        existing_record=existing_resolution.record if existing_resolution is not None else None,
    )

    logger.debug("Created/updated virtual machine: %s", virtual_machine)

    vm_id = (
        virtual_machine.get("id")
        if isinstance(virtual_machine, dict)
        else getattr(virtual_machine, "id", None)
    )
    if vm_id is not None:
        custom_fields = payload.get("custom_fields")
        await persist_sync_state_with_role_compensation(
            netbox_session,
            persistence=write_virtual_machine_sync_state(
                netbox_session,
                virtual_machine_id=vm_id,
                custom_fields=custom_fields if isinstance(custom_fields, dict) else None,
                overwrite_custom_fields=(
                    overwrite_flags is None or overwrite_flags.overwrite_vm_custom_fields
                ),
                proxmox_vm_name=(
                    proxmox_resource.get("name")
                    if isinstance(proxmox_resource, dict)
                    else proxmox_resource.name
                ),
                proxmox_last_synced_role_id=(
                    role_decision.snapshot_value if role_decision.write_snapshot else None
                ),
            ),
            virtual_machine_id=int(vm_id),
            previous_role_id=(
                relation_id(existing_record.get("role")) if existing_record is not None else None
            ),
            previous_snapshot_id=(snapshot_read.snapshot_id if snapshot_read is not None else None),
            expected_snapshot_id=(
                role_decision.snapshot_value if role_decision.write_snapshot else None
            ),
            role_write_applied=role_decision.write_role,
        )
        try:
            await sync_vm_cloudinit(
                netbox_session,
                vm_id=int(vm_id),
                proxmox_config=proxmox_config,
                overwrite_flags=overwrite_flags,
            )
        except Exception as exc:  # pragma: no cover - non-fatal
            logger.warning("cloud-init reflection failed for vm_id=%s: %s", vm_id, exc)

    return virtual_machine if isinstance(virtual_machine, dict) else virtual_machine.dict()
