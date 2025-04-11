import traceback

from fastapi import WebSocket, Depends, APIRouter
from typing import Annotated


import asyncio
from datetime import datetime

try:
    from pynetbox_api import RawNetBoxSession
except Exception as error:
    print(error)
    pass

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
from proxbox_api import ProxboxTagDep


# Proxmox Deps
from proxbox_api.routes.proxmox.nodes import ProxmoxNodeInterfacesDep
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep

router = APIRouter()

@router.get('/devices')
async def create_devices():
    return {
        "message": "Devices created"
    }
    
@router.get(
    '/devices/create',
    response_model=Device.SchemaList,
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def create_proxmox_devices(
    clusters_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    websocket: WebSocket = None,
    node: str | None = None,
    use_websocket: bool = False,
    use_css: bool = False
):
    # GET /api/plugins/proxbox/sync-processes/
    nb = RawNetBoxSession()
    start_time = datetime.now()
    sync_process = None
    
    try:    
        sync_process = nb.plugins.proxbox.__getattr__('sync-processes').create({
            'name': f"sync-devices-{start_time}",
            'sync_type': "devices",
            'status': "not-started",
            'started_at': str(start_time),
            'completed_at': None,
            'runtime': None,
            'tags': [tag.get('id', 0)],
        })

    except Exception as error:
        print(error)
        pass
    
    device_list: list = []
    
    for cluster_status in clusters_status:
        for node_obj in cluster_status.node_list:
            if use_websocket:
                await websocket.send_json(
                    {
                        'object': 'device',
                        'type': 'create',
                        'data': {
                            'completed': False,
                            'sync_status': return_status_html('syncing', use_css),
                            'rowid': node_obj.name,
                            'name': node_obj.name,
                            'netbox_id': None,
                            'manufacturer': None,
                            'role': None,
                            'cluster': cluster_status.mode.capitalize(),
                            'device_type': None,
                    }
                }
            )
            
            
            try:
                cluster_type = await asyncio.to_thread(lambda: ClusterType(
                    name=cluster_status.mode.capitalize(),
                    slug=cluster_status.mode,
                    description=f'Proxmox {cluster_status.mode} mode',
                    tags=[tag.get('id', None)]
                ))
                
                #cluster_type = await asyncio.to_thread(lambda: )
                cluster = await asyncio.to_thread(lambda: Cluster(
                    name=cluster_status.name,
                    type=cluster_type.get('id'),
                    description = f'Proxmox {cluster_status.mode} cluster.',
                    tags=[tag.get('id', None)]
                ))
                
                device_type = await asyncio.to_thread(lambda: DeviceType(bootstrap_placeholder=True))
                role = await asyncio.to_thread(lambda: DeviceRole(bootstrap_placeholder=True))
                site = await asyncio.to_thread(lambda: Site(bootstrap_placeholder=True))
                
                netbox_device = None
                if cluster is not None:
                    # TODO: Based on name.ip create Device IP Address
                    netbox_device = await asyncio.to_thread(lambda: Device(
                        name=node_obj.name,
                        tags=[tag.get('id', 0)],
                        cluster = cluster.get('id'),
                        status='active',
                        description=f'Proxmox Node {node_obj.name}',
                        device_type=device_type.get('id', None),
                        role=role.get('id', None),
                        site=site.get('id', None),
                    ))
                    
                print(f'netbox_device: {netbox_device}')
                
                if netbox_device is None and all([use_websocket, websocket]):
                    await websocket.send_json(
                        {
                            'object': 'device',
                            'type': 'create',
                            'data': {
                                'completed': True,
                                'increment_count': 'yes',
                                'sync_status': return_status_html('completed', use_css),
                                'rowid': node_obj.name,
                                'name': f"<a href='{netbox_device.get('display_url')}'>{netbox_device.get('name')}</a>",
                                'netbox_id': netbox_device.get('id'),
                                #'manufacturer': f"<a href='{netbox_device.get('manufacturer').get('url')}'>{netbox_device.get('manufacturer').get('name')}</a>",
                                'role': f"<a href='{netbox_device.get('role').get('url')}'>{netbox_device.get('role').get('name')}</a>",
                                'cluster': f"<a href='{netbox_device.get('cluster').get('url')}'>{netbox_device.get('cluster').get('name')}</a>",
                                'device_type': f"<a href='{netbox_device.get('device_type').get('url')}'>{netbox_device.get('device_type').get('model')}</a>",
                            }
                        }
                    )
                    
                    # If node, return only the node requested.
                    if node:
                        if node == node_obj.name:
                            return Device.SchemaList([netbox_device])
                        else:
                            continue
                        
                    # If not node, return all nodes.
                    elif not node:
                        device_list.append(netbox_device)

            except FastAPIException as error:
                traceback.print_exc()
                raise ProxboxException(
                    message="Unknown Error creating device in Netbox",
                    detail=f"Error: {str(error)}"
                )
            
            except Exception as error:
                traceback.print_exc()
                raise ProxboxException(
                    message="Unknown Error creating device in Netbox",
                    detail=f"Error: {str(error)}"
                )
    
    # Send end message to websocket to indicate that the creation of devices is finished.
    if all([use_websocket, websocket]):
        await websocket.send_json({'object': 'device', 'end': True})
    
    # Clear cache after creating devices.
    global_cache.clear_cache()
    
    if sync_process:
        end_time = datetime.now()
        sync_process.status = "completed"
        sync_process.completed_at = str(end_time)
        sync_process.runtime = float((end_time - start_time).total_seconds())
        sync_process.save()
    
    return Device.SchemaList(device_list)

ProxmoxCreateDevicesDep = Annotated[Device.SchemaList, Depends(create_proxmox_devices)]

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
