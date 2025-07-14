import traceback

from fastapi import WebSocket, Depends, APIRouter
from typing import Annotated


import asyncio
from datetime import datetime

# pynetbox API Imports (from v6.0.0 plugin uses pynetbox-api package)
from pynetbox_api.ipam.ip_address import IPAddress
from pynetbox_api.dcim.device import Device, DeviceRole, DeviceType
from pynetbox_api.dcim.interface import Interface
from pynetbox_api.dcim.site import Site
from pynetbox_api.virtualization.cluster import Cluster
from pynetbox_api.virtualization.cluster_type import ClusterType
from pynetbox_api.cache import global_cache

# Proxbox API Imports
from proxbox_api.exception import ProxboxException
from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.services.sync.devices import ProxmoxCreateDevicesDep

# Proxmox Deps
from proxbox_api.routes.proxmox.nodes import ProxmoxNodeInterfacesDep
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep

router = APIRouter()

@router.get('/devices')
async def get_devices():
    return {
        "message": "Devices created"
    }
    
@router.get(
    '/devices/create',
    response_model=Device.SchemaList,
    response_model_exclude={'websocket'},
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def create_devices(proxmox_create_devices_dep: ProxmoxCreateDevicesDep):
    return proxmox_create_devices_dep

async def create_interface_and_ip(
    tag: ProxboxTagDep,
    node_interface,
    node
):
    interface_type_mapping: dict = {
        'lo': 'loopback',
        'bridge': 'bridge',
        'bond': 'lag',
        'vlan': 'virtual',
    }
        
    node_cidr = getattr(node_interface, 'cidr', None)

    interface = Interface(
        device=node.get('id', 0),
        name=node_interface.iface,
        status='active',
        type=interface_type_mapping.get(node_interface.type, 'other'),
        tags=[tag.get('id', 0)],
    )
    
    try:
        interface_id = getattr(interface, 'id', interface.get('id', None))
    except:
        interface_id = None
        pass

    if node_cidr and interface_id:
        IPAddress(
            address=node_cidr,
            assigned_object_type='dcim.interface',
            assigned_object_id=int(interface_id),
            status='active',
            tags=[tag.get('id', 0)],
        )
    
    return interface

@router.get(
    '/devices/{node}/interfaces/create',
    response_model=Interface.SchemaList,
    response_model_exclude_none=True,
    response_model_exclude_unset=True
)
async def create_proxmox_device_interfaces(
    nodes: ProxmoxCreateDevicesDep,
    node_interfaces: ProxmoxNodeInterfacesDep,
):
    node = None
    for device in nodes:
        node = device[1][0]
        break

    return Interface.SchemaList(
        await asyncio.gather(
            *[create_interface_and_ip(node_interface, node) for node_interface in node_interfaces]
        )
    )

ProxmoxCreateDeviceInterfacesDep = Annotated[Interface.SchemaList, Depends(create_proxmox_device_interfaces)]  

@router.get('/devices/interfaces/create')
async def create_all_devices_interfaces(
    #nodes: ProxmoxCreateDevicesDep,
    #node_interfaces: ProxmoxNodeInterfacesDep,
):  
    return {
        'message': 'Endpoint currently not working. Use /devices/{node}/interfaces/create instead.'
    }
