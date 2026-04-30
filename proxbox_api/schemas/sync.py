"""Sync-related request schemas, including the overwrite flag container.

The flag set must stay in lock-step with `netbox_proxbox.constants.OVERWRITE_FIELDS`
on the netbox-proxbox plugin side. Any addition, removal, or reordering must be
applied on both sides; the order is also the order rendered in the plugin UI.
"""

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
    overwrite_device_role: bool = Field(default=True)
    overwrite_device_type: bool = Field(default=True)
    overwrite_device_tags: bool = Field(default=True)
    overwrite_device_status: bool = Field(default=True)
    overwrite_device_description: bool = Field(default=True)
    overwrite_device_custom_fields: bool = Field(default=True)

    # Virtual Machine
    overwrite_vm_role: bool = Field(default=True)
    overwrite_vm_tags: bool = Field(default=True)
    overwrite_vm_description: bool = Field(default=True)
    overwrite_vm_custom_fields: bool = Field(default=True)

    # Cluster
    overwrite_cluster_tags: bool = Field(default=True)
    overwrite_cluster_description: bool = Field(default=True)
    overwrite_cluster_custom_fields: bool = Field(default=True)

    # Node Interface
    overwrite_node_interface_tags: bool = Field(default=True)
    overwrite_node_interface_custom_fields: bool = Field(default=True)

    # Storage
    overwrite_storage_tags: bool = Field(default=True)

    # VM Interface
    overwrite_vm_interface_tags: bool = Field(default=True)
    overwrite_vm_interface_custom_fields: bool = Field(default=True)

    # IP Address
    overwrite_ip_status: bool = Field(default=True)
    overwrite_ip_tags: bool = Field(default=True)
    overwrite_ip_custom_fields: bool = Field(default=True)
