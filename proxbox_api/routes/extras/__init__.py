from fastapi import APIRouter, Depends
from fastapi import WebSocket
from typing import Annotated

from pynetbox_api.extras.custom_field import CustomField
import asyncio

router = APIRouter()
@router.get(
    '/extras/custom-fields/create',
    response_model=CustomField.SchemaList,
    response_model_exclude_none=True,
    response_model_exclude_unset=True
)
async def create_custom_fields(
    websocket = WebSocket
):
    custom_fields: list = [
        {
            "object_types": [
                "virtualization.virtualmachine"
            ],
            "type": "integer",
            "name": "proxmox_vm_id",
            "label": "VM ID",
            "description": "Proxmox Virtual Machine or Container ID",
            "ui_visible": "always",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox"
        },
        {
            "object_types": [
                "virtualization.virtualmachine"
            ],
            "type": "boolean",
            "name": "proxmox_start_at_boot",
            "label": "Start at Boot",
            "description": "Proxmox Start at Boot Option",
            "ui_visible": "always",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox"
        },
        {
            "object_types": [
                "virtualization.virtualmachine"
            ],
            "type": "boolean",
            "name": "proxmox_unprivileged_container",
            "label": "Unprivileged Container",
            "description": "Proxmox Unprivileged Container",
            "ui_visible": "if-set",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox"
        },
        {
            "object_types": [
                "virtualization.virtualmachine"
            ],
            "type": "boolean",
            "name": "proxmox_qemu_agent",
            "label": "QEMU Guest Agent",
            "description": "Proxmox QEMU Guest Agent",
            "ui_visible": "if-set",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox"
        },
        {
            "object_types": [
                "virtualization.virtualmachine"
            ],
            "type": "text",
            "name": "proxmox_search_domain",
            "label": "Search Domain",
            "description": "Proxmox Search Domain",
            "ui_visible": "if-set",
            "ui_editable": "hidden",
            "weight": 100,
            "filter_logic": "loose",
            "search_weight": 1000,
            "group_name": "Proxmox"
        }
    ]
    
    async def create_custom_field_task(custom_field: dict):
        return await asyncio.to_thread(lambda: CustomField(**custom_field))

    # Create Custom Fields
    return await asyncio.gather(*[
        create_custom_field_task(custom_field_dict)
        for custom_field_dict in custom_fields
    ])              

CreateCustomFieldsDep = Annotated[CustomField.SchemaList, Depends(create_custom_fields)]   