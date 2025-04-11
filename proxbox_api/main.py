import traceback

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from pydantic import BaseModel
from typing import Annotated, List


import asyncio



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
from pynetbox_api.extras.tag import Tags



# Proxbox API Imports
from proxbox_api.exception import ProxboxException
from proxbox_api import ProxboxTagDep


# Proxmox Routes
from proxbox_api.routes.proxmox import router as proxmox_router
from proxbox_api.routes.proxmox.cluster import (
    router as px_cluster_router,
    ClusterResourcesDep
)
from proxbox_api.routes.proxmox.nodes import router as px_nodes_router
from proxbox_api.routes.netbox import router as netbox_router
from proxbox_api.routes.virtualization import router as virtualization_router
from proxbox_api.routes.extras import router as extras_router, CreateCustomFieldsDep


# Sessions
from proxbox_api.session.proxmox import ProxmoxSessionsDep

from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines

# Proxmox Deps
from proxbox_api.routes.proxmox.nodes import (
    ProxmoxNodeDep,
    ProxmoxNodeInterfacesDep,
    get_node_network
)
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep

"""
CORS ORIGINS
"""

configuration = None
default_config: dict = {}
plugin_configuration: dict = {}
proxbox_cfg: dict = {}  

PROXBOX_PLUGIN_NAME: str = "netbox_proxbox"

# Init FastAPI
app = FastAPI(  
    title="Proxbox Backend",
    description="## Proxbox Backend made in FastAPI framework",
    version="0.0.1"
)


from sqlmodel import select
from pynetbox_api.database import NetBoxEndpoint, get_session
from sqlalchemy.exc import OperationalError

netbox_endpoint = None
database_session = None
try:
    database_session = next(get_session())
except OperationalError as error:
    print(error)
    pass

if database_session:    
    try:
        netbox_endpoints = database_session.exec(select(NetBoxEndpoint)).all()
    except OperationalError as error:
        # If table does not exist, create it.
        from pynetbox_api.database import create_db_and_tables
        create_db_and_tables()
        netbox_endpoints = database_session.exec(select(NetBoxEndpoint)).all()
        

origins = []
"""
CORS Middleware
"""
if netbox_endpoints:
    for netbox_endpoint in netbox_endpoints:
        protocol = "https" if netbox_endpoint.verify_ssl else "http"
        origins.extend([
            f"{protocol}://{netbox_endpoint.domain}",
            f"{protocol}://{netbox_endpoint.domain}:80",
            f"{protocol}://{netbox_endpoint.domain}:443",
            f"{protocol}://{netbox_endpoint.domain}:8000"
        ])
        
# Add default development origins
origins.extend([
    "https://127.0.0.1:443",
    "http://127.0.0.1:80", 
    "http://127.0.0.1:8000"
])

print(origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"]
)


@app.exception_handler(ProxboxException)
async def proxmoxer_exception_handler(request: Request, exc: ProxboxException):
    return JSONResponse(
        status_code=400,
        content={
            "message": exc.message,
            "detail": exc.detail,
            "python_exception": exc.python_exception,
        }
    )



@app.get("/")
async def standalone_info():
    return {
        "message": "Proxbox Backend made in FastAPI framework",
        "proxbox": {
            "github": "https://github.com/netdevopsbr/netbox-proxbox",
            "docs": "https://docs.netbox.dev.br",
        },
        "fastapi": {
            "github": "https://github.com/tiangolo/fastapi",
            "website": "https://fastapi.tiangolo.com/",
            "reason": "FastAPI was chosen because of performance and reliabilty."
        }
    }
    
from pynetbox_api.cache import global_cache

@app.get('/cache')
async def get_cache():
    return global_cache.return_cache()
 
@app.get('/dcim/devices')
async def create_devices():
    return {
        "message": "Devices created"
    }

@app.get('/clear-cache')
async def clear_cache():
    global_cache.clear_cache()
    return {
        "message": "Cache cleared"
    }


from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class SyncProcessIn(BaseModel):
    name: str
    sync_type: str
    status: str
    started_at: datetime
    completed_at: datetime

class SyncProcess(SyncProcessIn):
    id: int
    url: str
    display: str
    
# Example instance
example_sync_process = SyncProcess(
    id=1,
    url="https://10.0.30.200/api/plugins/proxbox/sync-processes/1/",
    display="teste (all)",
    name="teste",
    sync_type="all",
    status="not-started",
    started_at="2025-03-13T15:08:09.051478Z",
    completed_at="2025-03-13T15:08:09.051478Z",

)

@app.get('/sync-processes', response_model=List[SyncProcess])
async def get_sync_processes():
    """
    Get all sync processes from Netbox.
    """
    
    nb = RawNetBoxSession()
    sync_processes = [process.serialize() for process in nb.plugins.proxbox.__getattr__('sync-processes').all()]
    return sync_processes

@app.post('/sync-processes', response_model=SyncProcess)
async def create_sync_process():
    """
    Create a new sync process in Netbox.
    """
    
    print(datetime.now())
    
    nb = RawNetBoxSession
    sync_process = nb.plugins.proxbox.__getattr__('sync-processes').create(
        name=f"sync-process-{datetime.now()}",
        sync_type="all",
        status="not-started",
        started_at=str(datetime.now()),
        completed_at=str(datetime.now()),
    )
    
    return sync_process
'''
@app.websocket("/ws-test")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        await websocket.send_text(f"Successful")
''' 

@app.get(
    '/dcim/devices/create',
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

@app.get(
    '/dcim/devices/{node}/interfaces/create',
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

@app.get('/dcim/devices/interfaces/create')
async def create_all_devices_interfaces(
    #nodes: ProxmoxCreateDevicesDep,
    #node_interfaces: ProxmoxNodeInterfacesDep,
):  
    return {
        'message': 'Endpoint currently not working. Use /dcim/devices/{node}/interfaces/create instead.'
    }

    
#
# Routes (Endpoints)
#

# Netbox Routes
app.include_router(netbox_router, prefix="/netbox", tags=["netbox"])
#app.include_router(nb_dcim_router, prefix="/netbox/dcim", tags=["netbox / dcim"])
#app.include_router(nb_virtualization_router, prefix="/netbox/virtualization", tags=["netbox / virtualization"])

# Proxmox Routes
app.include_router(px_nodes_router, prefix="/proxmox/nodes", tags=["proxmox / nodes"])
app.include_router(px_cluster_router, prefix="/proxmox/cluster", tags=["proxmox / cluster"])
app.include_router(proxmox_router, prefix="/proxmox", tags=["proxmox"])
app.include_router(virtualization_router, prefix="/virtualization", tags=["virtualization"])
app.include_router(extras_router, prefix="/extras", tags=["extras"])

@app.websocket('/')
async def base_websocket(websocket: WebSocket):
    count = 0
    
    await websocket.accept()
    try:
        while True:
            #data = await websocket.receive_text()
            #await websocket.send_text(f"Message text was: {data}")
            count = count+1
            await websocket.send_text(f'Message: {count}')
            await asyncio.sleep(2)
            
    except WebSocketDisconnect:
        print("WebSocket connection closed")

@app.get("/ws-test-http")
async def websocket_endpoint(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
):
    print(NetboxSessionDep)
    print(ProxmoxSessionsDep)
    print(ClusterStatusDep)
    print(ClusterResourcesDep)
    print(CreateCustomFieldsDep)
    print(ProxboxTagDep)
    print('route ws-test-http reached')

@app.websocket("/ws/virtual-machines")
async def websocket_endpoint(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket: WebSocket,
):
    print('route ws/virtual-machines reached')
    
    connection_open = False
    
    try:
        await websocket.accept()
        connection_open = True
        await websocket.send_text('Connected!')
    except Exception as error:
        print(f"Error while accepting WebSocket connection: {error}")
        try:
            await websocket.close()
        except Exception as error:
            print(f"Error while closing WebSocket connection: {error}")
            
    data = None
    
    await create_virtual_machines(
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        custom_fields=custom_fields,
        websocket=websocket,
        tag=tag,
        use_css=False
    )
                

@app.get('/full-update')
async def full_update_sync(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep
):
    start_time = datetime.now()
    sync_process = None

    nb = RawNetBoxSession()
    try:
        sync_process = nb.plugins.proxbox.__getattr__('sync-processes').create(
            name=f"sync-all-{start_time}",
            sync_type="all",
            status="not-started",
            started_at=str(start_time),
            completed_at=None,
            runtime=None,
            tags=[tag.get('id', 0)],
        )
    except Exception as error:
        print(error)
        pass

    try:
        # Sync Nodes
        sync_nodes = await create_proxmox_devices(
            clusters_status=cluster_status,
                node=None,
                tag=tag,
                use_websocket=False
            )
    except Exception as error:
        print(error)
        raise ProxboxException(message=f"Error while syncing nodes.", python_exception=str(error))

    if sync_nodes: 
        # Sync Virtual Machines
        try:
            sync_vms = await create_virtual_machines(
                pxs=pxs,
                cluster_status=cluster_status,
                cluster_resources=cluster_resources,
                custom_fields=custom_fields,
                tag=tag,
                use_websocket=False
            )
        except Exception as error:
            print(error)
            raise ProxboxException(message=f"Error while syncing virtual machines.", python_exception=str(error))

    if sync_process:
        end_time = datetime.now()
        sync_process.status = "completed"
        sync_process.completed_at = str(end_time)
        sync_process.runtime = float((end_time - start_time).total_seconds())
        sync_process.save()
        
        print(sync_process)
        print(sync_process.runtime)
    return sync_nodes, sync_vms

    
@app.websocket("/ws")
async def websocket_endpoint(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket: WebSocket,
):
    connection_open = False
    
    nb = RawNetBoxSession()
    
    print('route ws reached')
    try:
        await websocket.accept()
        connection_open = True
        
        await websocket.send_text('Connected!')
    except Exception as error:
        print(f"Error while accepting WebSocket connection: {error}")
        try:
            await websocket.close()
        except Exception as error:
            print(f"Error while closing WebSocket connection: {error}")
    
    # 'data' is the message received from the WebSocket.
    data = None

    await websocket.send_text('Connected 2!')
    
    try:
        while True:
            try:
                data = await websocket.receive_text()
                print(f'Received message: {data}')
                await websocket.send_text(f'Received message: {data}')
            except Exception as error:
                print(f"Error while receiving data from WebSocket: {error}")
                break
            
            # Sync Nodes
            sync_nodes_function = create_proxmox_devices(
                clusters_status=cluster_status,
                node=None,
                websocket=websocket,
                tag=tag
            )
            
            # Sync Virtual Machines
            sync_vms_function = create_virtual_machines(
                pxs=pxs,
                cluster_status=cluster_status,
                cluster_resources=cluster_resources,
                custom_fields=custom_fields,
                websocket=websocket,
                tag=tag,
                use_websocket=True
            )
            
            if data == "Full Update Sync":
                sync_process = None
                
                try:
                    sync_process = nb.plugins.proxbox.__getattr__('sync-processes').create(
                        name=f"sync-process-{datetime.now()}",
                        sync_type="all",
                        status="not-started",
                        started_at=str(datetime.now()),
                    )
                except Exception as error:
                    print(error)
                    pass
                
                # Sync Nodes
                sync_nodes = await create_proxmox_devices(
                    clusters_status=cluster_status,
                    node=None,
                    websocket=websocket,
                    tag=tag,
                    use_websocket=True
                )
                
                if sync_nodes: 
                    # Sync Virtual Machines
                    await create_virtual_machines(
                        pxs=pxs,
                        cluster_status=cluster_status,
                        cluster_resources=cluster_resources,
                        custom_fields=custom_fields,
                        websocket=websocket,
                        tag=tag,
                        use_websocket=True
                    )
                
                if sync_process:
                    sync_process.status = "completed"
                    sync_process.completed_at = str(datetime.now())
                    sync_process.save()
                
            if data == "Sync Nodes":
                print('Sync Nodes')
                await websocket.send_text('Sync Nodes')
                await create_proxmox_devices(
                    clusters_status=cluster_status,
                    node=None,
                    websocket=websocket,
                    tag=tag,
                    use_websocket=True
                )
                
            elif data == "Sync Virtual Machines":
                await create_virtual_machines(
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    custom_fields=custom_fields,
                    websocket=websocket,
                    tag=tag,
                    use_websocket=True
                )
                
            else:
                await websocket.send_text(f"Invalid command: {data}")
                await websocket.send_text("Valid commands: 'Sync Nodes', 'Sync Virtual Machines', 'Full Update Sync'")
                #await websocket.send_denial_response("Invalid command.")

    except WebSocketDisconnect as error:
        print(f"WebSocket Disconnected: {error}")
        connection_open = False
    finally:
        if connection_open and websocket.client_state.CONNECTED:
            await websocket.close(code=1000, reason=None)
