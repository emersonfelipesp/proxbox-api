import asyncio
from fastapi import WebSocket, Depends
from typing import Annotated
from datetime import datetime
from proxbox_api import RawNetBoxSession
from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.utils import return_status_html
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep

from proxbox_api.exception import ProxboxException
from pynetbox_api.exceptions import FastAPIException

import traceback

from pynetbox_api.virtualization.cluster import Cluster, ClusterType
from pynetbox_api.dcim.device import Device
from pynetbox_api.dcim.device_type import DeviceType
from pynetbox_api.dcim.device_role import DeviceRole
from pynetbox_api.dcim.site import Site
from pynetbox_api.cache import global_cache

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