"""Sync-related request schemas, including the overwrite flag container.

The flag set must stay in lock-step with `netbox_proxbox.constants.OVERWRITE_FIELDS`
on the netbox-proxbox plugin side. Any addition, removal, or reordering must be
applied on both sides; the order is also the order rendered in the plugin UI.
"""

from collections.abc import Mapping

from pydantic import Field

from proxbox_api.schemas._base import ProxboxBaseModel


class SyncOverwriteFlags(ProxboxBaseModel):
    """Per-field overwrite gates forwarded from the plugin into reconcile services.

    Each flag controls whether the corresponding NetBox field is included in the
    `patchable_fields` set passed to `rest_reconcile_async` /
    `rest_bulk_reconcile_async`. `True` (the default) preserves historical
    always-overwrite behavior; `False` means the existing NetBox value is kept
    when reconciling.
    """

    # Device
    overwrite_device_role: bool = Field(
        default=True,
        title="Overwrite Device Role",
        description=(
            "When false, the device role is not patched on existing NetBox devices that "
            "already have a role. The role is still set when a device is first created."
        ),
    )
    overwrite_device_type: bool = Field(
        default=True,
        title="Overwrite Device Type",
        description=(
            "When false, the device type is not patched on existing NetBox devices that "
            "already have a device type. The type is still set when a device is first created."
        ),
    )
    overwrite_device_tags: bool = Field(
        default=True,
        title="Overwrite Device Tags",
        description=(
            "When false, tags are not patched on existing NetBox devices that already have "
            "tags. Tags are still applied when a device is first created."
        ),
    )
    overwrite_device_status: bool = Field(
        default=True,
        title="Overwrite Device Status",
        description=(
            "When false, the device status is not patched on existing NetBox devices. "
            "Status is still set when a device is first created."
        ),
    )
    overwrite_device_description: bool = Field(
        default=True,
        title="Overwrite Device Description",
        description=(
            "When false, the device description is not patched on existing NetBox devices "
            "that already have a non-empty description. Description is still set on first create."
        ),
    )
    overwrite_device_custom_fields: bool = Field(
        default=True,
        title="Overwrite Device Custom Fields",
        description=(
            "When false, custom_fields are not patched on existing NetBox devices that already "
            "have non-empty custom_fields. Custom fields are still applied on first create."
        ),
    )

    # Virtual Machine
    overwrite_vm_role: bool = Field(
        default=True,
        title="Overwrite VM Role",
        description=(
            "When false, the VM role is not patched on existing VMs that already have a role. "
            "The role is still set when a VM is first created."
        ),
    )
    overwrite_vm_type: bool = Field(
        default=True,
        title="Overwrite VM Type",
        description=(
            "When false, the VM type is not patched on existing VMs that already have a type. "
            "The type is still set when a VM is first created."
        ),
    )
    overwrite_vm_tags: bool = Field(
        default=True,
        title="Overwrite VM Tags",
        description=(
            "When false, tags are not patched on existing VMs that already have tags. "
            "Tags are still applied when a VM is first created."
        ),
    )
    overwrite_vm_description: bool = Field(
        default=True,
        title="Overwrite VM Description",
        description=(
            "When false, the VM description is not patched on existing VMs that already "
            "have a non-empty description. Description is still set on first create."
        ),
    )
    overwrite_vm_custom_fields: bool = Field(
        default=True,
        title="Overwrite VM Custom Fields",
        description=(
            "When false, custom_fields are not patched on existing VMs that already have "
            "non-empty custom_fields. Custom fields are still applied on first create."
        ),
    )

    # Cluster
    overwrite_cluster_tags: bool = Field(
        default=True,
        title="Overwrite Cluster Tags",
        description=(
            "When false, tags are not patched on existing NetBox clusters that already have "
            "tags. Tags are still applied when a cluster is first created."
        ),
    )
    overwrite_cluster_description: bool = Field(
        default=True,
        title="Overwrite Cluster Description",
        description=(
            "When false, the cluster description is not patched on existing NetBox clusters "
            "that already have a non-empty description."
        ),
    )
    overwrite_cluster_custom_fields: bool = Field(
        default=True,
        title="Overwrite Cluster Custom Fields",
        description=(
            "When false, custom_fields are not patched on existing NetBox clusters that "
            "already have non-empty custom_fields."
        ),
    )

    # Node Interface
    overwrite_node_interface_tags: bool = Field(
        default=True,
        title="Overwrite Node Interface Tags",
        description=(
            "When false, tags are not patched on existing NetBox node interfaces that "
            "already have tags."
        ),
    )
    overwrite_node_interface_custom_fields: bool = Field(
        default=True,
        title="Overwrite Node Interface Custom Fields",
        description=(
            "When false, custom_fields are not patched on existing NetBox node interfaces "
            "that already have non-empty custom_fields."
        ),
    )

    # Storage
    overwrite_storage_tags: bool = Field(
        default=True,
        title="Overwrite Storage Tags",
        description=(
            "When false, tags are not patched on existing NetBox storage objects that "
            "already have tags."
        ),
    )

    # VM Interface
    overwrite_vm_interface_tags: bool = Field(
        default=True,
        title="Overwrite VM Interface Tags",
        description=(
            "When false, tags are not patched on existing NetBox VM interfaces that "
            "already have tags."
        ),
    )
    overwrite_vm_interface_custom_fields: bool = Field(
        default=True,
        title="Overwrite VM Interface Custom Fields",
        description=(
            "When false, custom_fields are not patched on existing NetBox VM interfaces "
            "that already have non-empty custom_fields."
        ),
    )

    # IP Address
    overwrite_ip_status: bool = Field(
        default=True,
        title="Overwrite IP Status",
        description=(
            "When false, the IP status is not patched on existing NetBox IP address records."
        ),
    )
    overwrite_ip_tags: bool = Field(
        default=True,
        title="Overwrite IP Tags",
        description=(
            "When false, tags are not patched on existing NetBox IP address records that "
            "already have tags."
        ),
    )
    overwrite_ip_custom_fields: bool = Field(
        default=True,
        title="Overwrite IP Custom Fields",
        description=(
            "When false, custom_fields are not patched on existing NetBox IP address records "
            "that already have non-empty custom_fields."
        ),
    )
    overwrite_ip_address_dns_name: bool = Field(
        default=True,
        title="Overwrite IP Address DNS Name",
        description=(
            "When false, dns_name is not patched on existing NetBox IP address records, "
            "preserving any value the operator manually set in NetBox."
        ),
    )


def overwrite_flags_from_query_params(
    query_params: Mapping[str, object],
    base: SyncOverwriteFlags | None = None,
) -> SyncOverwriteFlags:
    """Resolve canonical overwrite flags from raw flat query parameters.

    FastAPI's ``Annotated[SyncOverwriteFlags, Query()]`` support has changed
    across framework/Pydantic releases. The plugin sends a flat query string, so
    make those raw keys authoritative whenever they are present.
    """
    resolved = (base or SyncOverwriteFlags()).model_dump()
    for name in SyncOverwriteFlags.model_fields:
        if name in query_params:
            resolved[name] = query_params[name]
    return SyncOverwriteFlags(**resolved)


class SyncBehaviorFlags(ProxboxBaseModel):
    """Opt-in sync behavior toggles forwarded from the netbox-proxbox plugin.

    Separate from ``SyncOverwriteFlags`` so the per-field overwrite contract
    (`netbox_proxbox.constants.OVERWRITE_FIELDS`) stays scoped to overwrite
    semantics. These flags govern other opt-in synchronization behaviors.
    """

    parse_description_metadata: bool = Field(
        default=False,
        title="Parse Description Metadata",
        description=(
            "When true, the sync reads Proxmox descriptions for a fenced "
            "``netbox-metadata`` JSON block (e.g. ``{\"tenant\": 13, \"site\": 4}``) "
            "and applies the resulting NetBox PK overrides to the synced object. "
            "When false, the Proxmox description is ignored exactly as it was "
            "before this feature shipped."
        ),
    )


def behavior_flags_from_query_params(
    query_params: Mapping[str, object],
    base: SyncBehaviorFlags | None = None,
) -> SyncBehaviorFlags:
    """Resolve canonical behavior flags from raw flat query parameters."""
    resolved = (base or SyncBehaviorFlags()).model_dump()
    for name in SyncBehaviorFlags.model_fields:
        if name in query_params:
            resolved[name] = query_params[name]
    return SyncBehaviorFlags(**resolved)
