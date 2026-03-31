"""Extras route handlers for NetBox custom field management."""

from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket

from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxCustomFieldSyncState
from proxbox_api.session.netbox import NetBoxAsyncSessionDep

router = APIRouter()


@router.get(
    "/extras/custom-fields/create",
    response_model=list[dict],
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def create_custom_fields(
    netbox_session: NetBoxAsyncSessionDep,
    websocket=WebSocket,
):
    custom_fields: list = [
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "integer",
            "name": "proxmox_vm_id",
            "label": "VM ID",
            "description": "Proxmox Virtual Machine or Container ID",
            "ui_visible": "always",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox",
        },
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "text",
            "name": "proxmox_vm_type",
            "label": "VM Type",
            "description": "Proxmox VM type (qemu or lxc)",
            "ui_visible": "always",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox",
        },
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "boolean",
            "name": "proxmox_start_at_boot",
            "label": "Start at Boot",
            "description": "Proxmox Start at Boot Option",
            "ui_visible": "always",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox",
        },
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "boolean",
            "name": "proxmox_unprivileged_container",
            "label": "Unprivileged Container",
            "description": "Proxmox Unprivileged Container",
            "ui_visible": "if-set",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox",
        },
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "boolean",
            "name": "proxmox_qemu_agent",
            "label": "QEMU Guest Agent",
            "description": "Proxmox QEMU Guest Agent",
            "ui_visible": "if-set",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox",
        },
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "text",
            "name": "proxmox_search_domain",
            "label": "Search Domain",
            "description": "Proxmox Search Domain",
            "ui_visible": "if-set",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox",
        },
        {
            "object_types": [
                "virtualization.virtualmachine",
                "dcim.device",
                "virtualization.cluster",
                "virtualization.cluster-type",
                "dcim.device-type",
                "dcim.device-role",
                "dcim.manufacturer",
                "dcim.site",
                "virtualization.vminterface",
                "ipam.ip-address",
                "virtualization.virtual-disk",
            ],
            "type": "datetime",
            "name": "proxmox_last_updated",
            "label": "Last Updated",
            "description": "Proxmox Plugin last modified this object",
            "ui_visible": "always",
            "ui_editable": "hidden",
            "weight": 200,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox",
        },
    ]
    fields = []

    for custom_field in custom_fields:
        record = await rest_reconcile_async(
            netbox_session,
            "/api/extras/custom-fields/",
            lookup={"name": custom_field["name"]},
            payload=custom_field,
            schema=NetBoxCustomFieldSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "type": record.get("type"),
                "label": record.get("label"),
                "description": record.get("description"),
                "ui_visible": record.get("ui_visible"),
                "ui_editable": record.get("ui_editable"),
                "weight": record.get("weight"),
                "filter_logic": record.get("filter_logic"),
                "search_weight": record.get("search_weight"),
                "group_name": record.get("group_name"),
                "object_types": record.get("object_types"),
            },
        )
        fields.append(record.serialize())
    return fields


CreateCustomFieldsDep = Annotated[list[dict], Depends(create_custom_fields)]
