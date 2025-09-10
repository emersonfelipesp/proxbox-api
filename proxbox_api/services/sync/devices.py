import asyncio
from fastapi import WebSocket, Depends
from typing import Annotated
from datetime import datetime
from proxbox_api.session.netbox import NetBoxSessionDep
from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.utils import return_status_html, sync_process
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

from proxbox_api.logger import logger

@sync_process(sync_type='devices')
async def create_proxmox_devices(
    netbox_session: NetBoxSessionDep,
    clusters_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    websocket: WebSocket = None,
    node: str | None = None,
    use_websocket: bool = False,
    use_css: bool = False,
    sync_process = None,
):
    tag_id = getattr(tag, 'id', 0)
    tags = [tag_id] if tag_id > 0 else []
    
    # GET /api/plugins/proxbox/sync-processes/
    nb = netbox_session
    start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    journal_messages = []  # Store all journal messages
    total_devices = 0  # Track total devices processed
    successful_devices = 0  # Track successful device creations
    failed_devices = 0  # Track failed device creations
    
    journal_messages.append("## Device Sync Process Started")
    logger.info(f"Device Sync Process Started")
    journal_messages.append(f"- **Start Time**: {start_time}")
    journal_messages.append("- **Status**: Initializing")
    
    device_list: list = []
    
    try:
        journal_messages.append("\n## Device Discovery")
        
        # Count total devices to process (just for journalling)
        for cluster_status in clusters_status:
            if cluster_status and cluster_status.node_list:
                device_count = len(cluster_status.node_list)
                total_devices += device_count
                
                journal_messages.append(f"- Cluster `{cluster_status.name}` ({cluster_status.mode}): Found {device_count} devices")
        
        journal_messages.append(f"\n## Device Processing")
        journal_messages.append(f"- Total devices to process: {total_devices}")
        
        for cluster_status in clusters_status:
            if not cluster_status or not cluster_status.node_list:
                continue
                
            journal_messages.append(f"\n### 🔄Processing Cluster: {cluster_status.name}")
            logger.info(f"🔄 Processing Cluster: {cluster_status.name}")
            
            journal_messages.append(f"- Cluster Mode: {cluster_status.mode}")
            journal_messages.append(f"- Devices in cluster: {len(cluster_status.node_list)}")
            
            for node_obj in cluster_status.node_list:
                device_name = node_obj.name
                
                journal_messages.append(f"\n#### 🔄 Processing Device: {device_name}")
                logger.info(f"🔄 Processing Device: {device_name}")
                
                if use_websocket and websocket:
                    await websocket.send_json({
                        'object': 'device',
                        'type': 'create',
                        'data': {
                            'completed': False,
                            'sync_status': return_status_html('syncing', use_css),
                            'rowid': device_name,
                            'name': device_name,
                            'netbox_id': None,
                            'manufacturer': None,
                            'role': None,
                            'cluster': cluster_status.mode.capitalize(),
                            'device_type': None,
                        }
                    })
                
                try:
                    journal_messages.append(f"- Creating cluster type: {cluster_status.mode.capitalize()}")
                    cluster_type = await asyncio.to_thread(lambda: ClusterType(
                        name=cluster_status.mode.capitalize(),
                        slug=cluster_status.mode,
                        description=f'Proxmox {cluster_status.mode} mode',
                        tags=tags
                    ))
                    
                    journal_messages.append(f"- Creating cluster: {cluster_status.name}")
                    cluster = await asyncio.to_thread(lambda: Cluster(
                        name=cluster_status.name,
                        type=getattr(cluster_type, 'id', None),
                        description = f'Proxmox {cluster_status.mode} cluster.',
                        tags=tags
                    ))
                    
                    journal_messages.append(f"- Creating device type, role, and site placeholders")
                    device_type = await asyncio.to_thread(lambda: DeviceType(bootstrap_placeholder=True))
                    role = await asyncio.to_thread(lambda: DeviceRole(bootstrap_placeholder=True))
                    site = await asyncio.to_thread(lambda: Site(bootstrap_placeholder=True))
                    
                    netbox_device = None
                    
                    if cluster is not None: 
                        journal_messages.append(f"- Creating device: {device_name}")
                        # TODO: Based on name.ip create Device IP Address
                        netbox_device = await asyncio.to_thread(lambda: Device(
                            name=device_name,
                            tags=tags,
                            cluster = getattr(cluster, 'id', None),
                            status='active',
                            description=f'Proxmox Node {device_name}',
                            device_type=getattr(device_type, 'id', None),
                            role=getattr(role, 'id', None),
                            site=getattr(site, 'id', None),
                        ))
                        
                        journal_messages.append(f"- ✅ Device created/synced successfully: {device_name}")
                        logger.info(f"✅ Device created/synced successfully: {device_name}")
                        
                    if netbox_device:
                        # If node, return only the node requested.
                        if node and node == device_name:
                            journal_messages.append(f"- Returning single device: {device_name}")
                            
                            return Device.SchemaList([netbox_device.json]) if netbox_device.json else Device.SchemaList([])
                        
                        device_list.append(netbox_device.json)
                        successful_devices += 1
                        journal_messages.append(f"- ✅ Successfully created device: {device_name} (ID: {netbox_device.get('id')})")
                        
                        if use_websocket and websocket:
                            await websocket.send_json(
                                {
                                    'object': 'device',
                                    'type': 'create',
                                    'data': {
                                        'completed': True,
                                        'increment_count': 'yes',
                                        'sync_status': return_status_html('completed', use_css),
                                        'rowid': device_name,
                                        'name': f"<a href='{netbox_device.get('display_url')}'>{netbox_device.get('name')}</a>",
                                        'netbox_id': netbox_device.get('id'),
                                        #'manufacturer': f"<a href='{netbox_device.get('manufacturer').get('url')}'>{netbox_device.get('manufacturer').get('name')}</a>",
                                        'role': f"<a href='{netbox_device.get('role').get('url')}'>{netbox_device.get('role').get('name')}</a>",
                                        'cluster': f"<a href='{netbox_device.get('cluster').get('url')}'>{netbox_device.get('cluster').get('name')}</a>",
                                        'device_type': f"<a href='{netbox_device.get('device_type').get('url')}'>{netbox_device.get('device_type').get('model')}</a>",
                                    }
                                }
                            )
                    else:
                        failed_devices += 1
                        error_msg = f"Device creation failed for {device_name}. netbox_device is None."
                        journal_messages.append(f"- ❌ {error_msg}")
                        
                        if use_websocket and websocket:
                            # Handle the case where netbox_device is None
                            await websocket.send_json(
                                {
                                    'object': 'device',
                                    'type': 'create',
                                    'data': {
                                        'completed': False,
                                        'increment_count': 'no',
                                        'sync_status': return_status_html('failed', use_css),
                                        'rowid': device_name,
                                        'error': error_msg,
                                    }
                                }
                            )
                
                except Exception as error:
                    failed_devices += 1
                    error_msg = f"Error creating device {device_name}: {str(error)}"
                    journal_messages.append(f"- ❌ {error_msg}")
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
        
        journal_messages.append(f"\n## Process Summary")
        journal_messages.append(f"- **Status**: {getattr(sync_process, 'status', 'unknown')}")
        journal_messages.append(f"- **Runtime**: {getattr(sync_process, 'runtime', 'unknown')} seconds")
        journal_messages.append(f"- **End Time**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        journal_messages.append(f"- **Total Devices Processed**: {total_devices}")
        journal_messages.append(f"- **Successfully Created**: {successful_devices}")
        journal_messages.append(f"- **Failed**: {failed_devices}")
        
        
    except Exception as error:
        error_msg = f"Error during device sync: {str(error)}"
        journal_messages.append(f"\n### ❌ Error\n{error_msg}")
        raise ProxboxException(message=error_msg)
    
    finally:
        # Create journal entry
        try:
            if sync_process and hasattr(sync_process, 'id'):
                journal_entry = nb.extras.journal_entries.create({
                    'assigned_object_type': 'netbox_proxbox.syncprocess',
                    'assigned_object_id': sync_process.id,
                    'kind': 'info',
                    'comments': '\n'.join(journal_messages),
                    'tags': tags
                })
                
                if not journal_entry:
                    print("Warning: Journal entry creation returned None")
            else:
                print("Warning: Cannot create journal entry - sync_process is None or has no id")
        except Exception as journal_error:
            print(f"Warning: Failed to create journal entry: {str(journal_error)}")
    
    return Device.SchemaList(device_list)

ProxmoxCreateDevicesDep = Annotated[Device.SchemaList, Depends(create_proxmox_devices)]