"""DCIM route handlers for device and interface synchronization."""

import traceback

from fastapi import WebSocket, Depends, APIRouter
from typing import Annotated


import asyncio
from datetime import datetime

# NetBox compatibility wrappers
from proxbox_api.netbox_compat import (
    IPAddress,
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Site,
    Cluster,
    ClusterType,
)
from proxbox_api.cache import global_cache

# Proxbox API Imports
from proxbox_api.exception import ProxboxException
from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.services.sync.devices import ProxmoxCreateDevicesDep

# Proxmox Deps
from proxbox_api.routes.proxmox.nodes import ProxmoxNodeInterfacesDep
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep

router = APIRouter()


@router.get("/devices")
async def get_devices():
    return {"message": "Devices created"}


@router.get(
    "/devices/create",
    response_model=list[dict],
    response_model_exclude={"websocket"},
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def create_devices(proxmox_create_devices_dep: ProxmoxCreateDevicesDep):
    return proxmox_create_devices_dep


async def create_interface_and_ip(tag: ProxboxTagDep, node_interface, node):
    interface_type_mapping: dict = {
        "lo": "loopback",
        "bridge": "bridge",
        "bond": "lag",
        "vlan": "virtual",
    }

    node_cidr = getattr(node_interface, "cidr", None)

    node_data = node if isinstance(node, dict) else {}

    interface = Interface(
        device=node_data.get("id", 0),
        name=node_interface.iface,
        status="active",
        type=interface_type_mapping.get(node_interface.type, "other"),
        tags=[getattr(tag, "id", 0)],
    )

    try:
        interface_id = getattr(interface, "id", interface.get("id", None))
    except:
        interface_id = None
        pass

    if node_cidr and interface_id is not None:
        IPAddress(
            address=node_cidr,
            assigned_object_type="dcim.interface",
            assigned_object_id=int(interface_id),
            status="active",
            tags=[getattr(tag, "id", 0)],
        )

    return interface


@router.get(
    "/devices/{node}/interfaces/create",
    response_model=list[dict],
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def create_proxmox_device_interfaces(
    node: str,
    nodes: ProxmoxCreateDevicesDep,
    tag: ProxboxTagDep,
    node_interfaces: ProxmoxNodeInterfacesDep,
):
    node = None
    for device in nodes:
        node = device
        break

    interfaces = await asyncio.gather(
        *[
            create_interface_and_ip(tag, node_interface, node)
            for node_interface in node_interfaces
        ]
    )
    return [
        interface.dict() if hasattr(interface, "dict") else interface
        for interface in interfaces
    ]


ProxmoxCreateDeviceInterfacesDep = Annotated[
    list[dict], Depends(create_proxmox_device_interfaces)
]


@router.get("/devices/interfaces/create")
async def create_all_devices_interfaces(
    # nodes: ProxmoxCreateDevicesDep,
    # node_interfaces: ProxmoxNodeInterfacesDep,
):
    return {
        "message": "Endpoint currently not working. Use /devices/{node}/interfaces/create instead."
    }
