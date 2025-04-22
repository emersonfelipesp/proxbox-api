# FastAPI Imports
from fastapi import APIRouter
from fastapi import WebSocket, Query
from typing import Annotated

from datetime import datetime
import asyncio

from proxbox_api.routes.proxmox.cluster import ClusterStatusDep, ClusterResourcesDep # Cluster Status and Resources
from proxbox_api.schemas.virtualization import ( # Schemas
    CPU, Memory, Disk, Network,
    Snapshot, Backup,
    VirtualMachineSummary,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep # Sessions
from proxbox_api.routes.extras import CreateCustomFieldsDep # Create Custom Fields
from proxbox_api.dependencies import ProxboxTagDep # Proxbox Tag
from proxbox_api.utils import return_status_html # Return Status HTML
from proxbox_api.routes.proxmox import get_vm_config # Get VM Config
from proxbox_api.exception import ProxboxException # Proxbox Exception

# pynetbox_api Imports
from pynetbox_api import RawNetBoxSession   # Raw NetBox Session
from pynetbox_api.virtualization.virtual_machine import VirtualMachine # Virtual Machine
from pynetbox_api.virtualization.cluster import Cluster # Cluster
from pynetbox_api.dcim.device import Device # Device
from pynetbox_api.dcim.device_role import DeviceRole # Device Role
from pynetbox_api.virtualization.interface import VMInterface # VM Interface
from pynetbox_api.ipam.ip_address import IPAddress # IP Address
from pynetbox_api.cache import global_cache # Global Cache

from proxbox_api.routes.proxmox import get_proxmox_node_storage_content # Get Proxmox Node Storage Content

router = APIRouter()

router.get('/virtual-machines/create')
async def create_virtual_machines(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket = WebSocket,
    use_css: bool = False,
    use_websocket: bool = False
):
    '''
    Creates a new virtual machine in Netbox.
    '''
    
    # GET /api/plugins/proxbox/sync-processes/
    nb = RawNetBoxSession()
    start_time = datetime.now()
    sync_process = None
    try:
        sync_process = nb.plugins.proxbox.__getattr__('sync-processes').create(
            name=f"sync-virtual-machines-{start_time}",
            sync_type="virtual-machines",
            status="not-started",
            started_at=str(start_time),
            completed_at=None,
            runtime=None,
            tags=[tag.get('id', 0)],
        )
    except Exception as error:
        print(error)
        pass
    
    async def _create_vm(cluster: dict):
        tasks = []  # Collect coroutines
        for cluster_name, resources in cluster.items():
            for resource in resources:
                if resource.get('type') in ('qemu', 'lxc'):
                    tasks.append(create_vm_task(cluster_name, resource))

        return await asyncio.gather(*tasks)  # Gather coroutines

    async def create_vm_task(cluster_name, resource):
        undefined_html = return_status_html('undefined', use_css)
        
        websocket_vm_json: dict = {
            'sync_status': return_status_html('syncing', use_css),
            'name': undefined_html,
            'netbox_id': undefined_html,
            'status': undefined_html,
            'cluster': undefined_html,
            'device': undefined_html,
            'role': undefined_html,
            'vcpus': undefined_html,
            'memory': undefined_html,
            'disk': undefined_html,
            'vm_interfaces': undefined_html
        }
        
        vm_role_mapping: dict = {
            'qemu': {
                'name': 'Virtual Machine (QEMU)',
                'slug': 'virtual-machine-qemu',
                'color': '00ffff',
                'description': 'Proxmox Virtual Machine',
                'tags': [tag.get('id', 0)],
                'vm_role': True
            },
            'lxc': {
                'name': 'Container (LXC)',
                'slug': 'container-lxc',
                'color': '7fffd4',
                'description': 'Proxmox LXC Container',
                'tags': [tag.get('id', 0)],
                'vm_role': True
            },
            'undefined': {
                'name': 'Unknown',
                'slug': 'unknown',
                'color': '000000',
                'description': 'VM Type not found. Neither QEMU nor LXC.',
                'tags': [tag.get('id', 0)],
                'vm_role': True
            }
        }
        
        #vm_config = px.session.nodes(resource.get("node")).qemu(resource.get("vmid")).config.get()
     
        vm_type = resource.get('type', 'unknown')
        vm_config = await get_vm_config(
            pxs=pxs,
            cluster_status=cluster_status,
            node=resource.get("node"),
            type=vm_type,
            vmid=resource.get("vmid")
        )
        
 
        start_at_boot = True if vm_config.get('onboot', 0) == 1 else False
        qemu_agent = True if vm_config.get('agent', 0) == 1 else False
        unprivileged_container = True if vm_config.get('unprivileged', 0) == 1 else False
        search_domain = vm_config.get('searchdomain', None)
        
        #print(f'vm_config: {vm_config}')
        
        
        initial_vm_json = websocket_vm_json | {
            'completed': False,
            'rowid': str(resource.get('name')),
            'name': str(resource.get('name')),
            'cluster': str(cluster_name),
            'device': str(resource.get('node')),
        }

        if all([use_websocket, websocket]):
            await websocket.send_json(
                {
                    'object': 'virtual_machine',
                    'type': 'create',
                    'data': initial_vm_json
                })

        try:
            print('\n')
            print('Creating Virtual Machine Dependents')
            cluster = await asyncio.to_thread(lambda: Cluster(name=cluster_name))
            device = await asyncio.to_thread(lambda: Device(name=resource.get('node')))
            role = await asyncio.to_thread(lambda: DeviceRole(**vm_role_mapping.get(vm_type)))
            
            
            print('> Virtual Machine Name: ', resource.get('name'))
            print('> Cluster: ', cluster.get('name'), cluster.get('id'), type(cluster.get('id')))
            print('> Device: ', device.get('name'), device.get('id'), type(device.get('id')))
            print('> Tag: ', tag.get('name'), tag.get('id'))
            print('> Role: ', role.get('name'), role.get('id'))
            print('Finish creating Virtual Machine Dependents')
            print('\n')
        except Exception as error:
            raise ProxboxException(
                message="Error creating Virtual Machine dependent objects (cluster, device, tag and role)",
                python_exception=f"Error: {str(error)}"
            )
            
        try:
            virtual_machine = await asyncio.to_thread(lambda: VirtualMachine(
                name=resource.get('name'),
                status=VirtualMachine.status_field.get(resource.get('status'), 'active'),
                cluster=cluster.get('id'),
                device=device.get('id'),
                vcpus=int(resource.get("maxcpu", 0)),
                memory=int(resource.get("maxmem")) // 1000000,  # Fixed typo 'mexmem'
                disk=int(resource.get("maxdisk", 0)) // 1000000,
                tags=[tag.get('id', 0)],
                role=role.get('id', 0),
                custom_fields={
                    "proxmox_vm_id": resource.get('vmid'),
                    "proxmox_start_at_boot": start_at_boot,
                    "proxmox_unprivileged_container": unprivileged_container,
                    "proxmox_qemu_agent": qemu_agent,
                    "proxmox_search_domain": search_domain,
                },
            ))

            
        except ProxboxException:
            raise
        except Exception as error:
            print(f'Error creating Virtual Machine in Netbox: {str(error)}')
            raise ProxboxException(
                message="Error creating Virtual Machine in Netbox",
                python_exception=f"Error: {str(error)}"
            )
            
        
        if type(virtual_machine) != dict:
            virtual_machine = virtual_machine.dict()
        
        def format_to_html(json: dict, key: str):
            return f"<a href='{json.get(key).get('url')}'>{json.get(key).get('name')}</a>"
        
        cluster_html = format_to_html(virtual_machine, 'cluster')
        device_html = format_to_html(virtual_machine, 'device')
        role_html = format_to_html(virtual_machine, 'role')
        
        
        active_raw = "Active"
        active_css = "<span class='text-bg-green badge p-1'>Active</span>"
        active_html = active_css if use_css else active_raw
        
        offline_raw = "Offline"
        offline_css = "<span class='text-bg-red badge p-1'>Offline</span>"
        offline_html = offline_css if use_css else offline_raw
        
        unknown_raw = "Unknown"
        unknown_css = "<span class='text-bg-grey badge p-1'>Unknown</span>"
        unknown_html = unknown_css if use_css else unknown_raw
        
        status_html_choices = {
            'active': active_html,
            'offline': offline_html,
            'unknown': unknown_html
        }
        
        status_html = status_html_choices.get(virtual_machine.get('status').get('value'), status_html_choices.get('unknown'))
    
        name_html_css = f"<a href='{virtual_machine.get('display_url')}'>{virtual_machine.get('name')}</a>"
        name_html_raw = f"{virtual_machine.get('name')}"
        name_html = name_html_css if use_css else name_html_raw
        
        vm_created_json: dict = initial_vm_json | {
            'increment_count': 'yes',
            'completed': True,
            'sync_status': return_status_html('completed', use_css),
            'rowid': str(resource.get('name')),
            'name': name_html,
            'netbox_id': virtual_machine.get('id'),
            'status': status_html,
            'cluster': cluster_html,
            'device': device_html,
            'role': role_html,
            'vcpus': virtual_machine.get('vcpus'),
            'memory': virtual_machine.get('memory'),
            'disk': virtual_machine.get('disk'),
            'vm_interfaces': [],
        }
        
        # At this point, the Virtual Machine was created in NetBox. Left to create the interfaces.
        if all([use_websocket, websocket]):
            await websocket.send_json(
                {
                    'object': 'virtual_machine',
                    'type': 'create',
                    'data': vm_created_json
                }
            )
        
        netbox_vm_interfaces: list = []
        
        if virtual_machine and vm_config:
            ''' 
            Create Virtual Machine Interfaces
            '''
            vm_networks: list = []
            network_id: int = 0 # Network ID
            while True:
                # Parse network information got from Proxmox to dict
                network_name = f'net{network_id}'
                
                vm_network_info = vm_config.get(network_name, None) # Example result: virtio=CE:59:22:67:69:b2,bridge=vmbr1,queues=20,tag=2010 
                if vm_network_info is not None:
                    net_fields = vm_network_info.split(',') # Example result: ['virtio=CE:59:22:67:69:b2', 'bridge=vmbr1', 'queues=20', 'tag=2010']
                    network_dict = dict([field.split('=') for field in net_fields]) # Example result: {'virtio': 'CE:59:22:67:69:b2', 'bridge': 'vmbr1', 'queues': '20', 'tag': '2010'}
                    vm_networks.append({network_name:network_dict})
                    
                    network_id += 1
                else:
                    # If no network found by increasing network id, break the loop.
                    break
            
            if vm_networks:
                for network in vm_networks:
                    # Parse the dict to valid netbox interface fields and Create Virtual Machine Interfaces
                    for interface_name, value in network.items():
                        # If 'bridge' value exists, create a bridge interface.
                        bridge_name = value.get('bridge', None)
                        bridge: dict = {}
                        if bridge_name:
                            bridge=VMInterface(
                                name=bridge_name,
                                virtual_machine=virtual_machine.get('id'),
                                type='bridge',
                                description=f'Bridge interface of Device {resource.get("node")}. The current NetBox modeling does not allow correct abstraction of virtual bridge.',
                                tags=[tag.get('id', 0)]
                            )
                        
                        if type(bridge) != dict:
                            bridge = bridge.dict()
                        
                        vm_interface = await asyncio.to_thread(lambda: VMInterface(
                            virtual_machine=virtual_machine.get('id'),
                            name=value.get('name', interface_name),
                            enabled=True,
                            bridge=bridge.get('id', None),
                            mac_address= value.get('virtio', value.get('hwaddr', None)), # Try get MAC from 'virtio' first, then 'hwaddr'. Else None.
                            tags=[tag.get('id', 0)]
                        ))
                        
                        
                        if type(vm_interface) != dict:
                            vm_interface = vm_interface.dict()
                        
                        netbox_vm_interfaces.append(vm_interface)
                        
                        # If 'ip' value exists and is not 'dhcp', create IP Address on NetBox.
                        interface_ip = value.get('ip', None)
                        if interface_ip and interface_ip != 'dhcp':
                            IPAddress(
                                address=interface_ip,
                                assigned_object_type='virtualization.vminterface',
                                assigned_object_id=vm_interface.get('id'),
                                status='active',
                                tags=[tag.get('id', 0)],
                            )
                            
                        # TODO: Create VLANs and other network related objects.
                        # 'tag' is the VLAN ID.
                        # 'bridge' is the bridge name.
        
        
        
        vm_created_with_interfaces_json: dict = vm_created_json | {
            'vm_interfaces': [f"<a href='{interface.get('display_url')}'>{interface.get('name')}</a>" for interface in netbox_vm_interfaces],
        }
        # Remove 'completed' and 'increment_count' keys from the dictionary so it does not affect progress count on GUI.
        vm_created_with_interfaces_json.pop('completed')
        vm_created_with_interfaces_json.pop('increment_count')
        
        if all([use_websocket, websocket]):
            await websocket.send_json(
                {
                    'object': 'virtual_machine',
                    'type': 'create',
                    'data': vm_created_with_interfaces_json
                }
            )
        
        
        # Lamba is necessary to treat the object instantiation as a coroutine/function.
        return virtual_machine

        """""
        proxmox_start_at_boot": resource.get(''),
        "proxmox_unprivileged_container": unprivileged_container,
        "proxmox_qemu_agent": qemu_agent,
        "proxmox_search_domain": search_domain,
        """
    
    
    
    # Return the created virtual machines.
    result_list = await asyncio.gather(*[_create_vm(cluster) for cluster in cluster_resources], return_exceptions=True)

    print('result_list: ', result_list)

    # Send end message to websocket to indicate that the creation of virtual machines is finished.
    if all([use_websocket, websocket]):
        await websocket.send_json({'object': 'virtual_machine', 'end': True})

    # Clear cache after creating virtual machines.
    global_cache.clear_cache()
    
    if sync_process:
        end_time = datetime.now()
        sync_process.status = "completed"
        sync_process.completed_at = str(end_time)
        sync_process.runtime = float((end_time - start_time).total_seconds())
        sync_process.save()
    
    return result_list
 
 
@router.get(
    '/virtual-machines/',
    response_model=VirtualMachine.SchemaList,
    response_model_exclude_none=True,
    response_model_exclude_unset=True
)
async def get_virtual_machines():
    virtual_machine = VirtualMachine()
    return virtual_machine.all()


@router.get(
    '/virtual-machines/{id}',
    response_model=VirtualMachine.Schema,
    response_model_exclude_none=True,
    response_model_exclude_unset=True
)
async def get_virtual_machine(id: int):
    try:
        virtual_machine = VirtualMachine().get(id=id)
        if virtual_machine:
            return virtual_machine
        else:
            return {}
    except Exception as error:
        print(f'Error getting virtual machine: {error}')
        return {}


        
@router.get(
    '/virtual-machines/summary/example',
    response_model=VirtualMachineSummary,
    response_model_exclude_none=True,
    response_model_exclude_unset=True
)
async def get_virtual_machine_summary_example():
   

    # Example usage
    vm_summary = VirtualMachineSummary(
        id="vm-102",
        name="db-server-01",
        status="running",
        node="pve-node-02",
        cluster="Production Cluster",
        os="CentOS 8",
        description="Primary database server for production applications",
        uptime="43 days, 7 hours, 12 minutes",
        created="2023-01-15",
        cpu=CPU(cores=8, sockets=1, type="host", usage=32),
        memory=Memory(total=16384, used=10240, usage=62),
        disks=[
            Disk(id="scsi0", storage="local-lvm", size=102400, used=67584, usage=66, format="raw", path="/dev/pve/vm-102-disk-0"),
            Disk(id="scsi1", storage="local-lvm", size=409600, used=215040, usage=52, format="raw", path="/dev/pve/vm-102-disk-1"),
        ],
        networks=[
            Network(id="net0", model="virtio", bridge="vmbr0", mac="AA:BB:CC:DD:EE:FF", ip="10.0.0.102", netmask="255.255.255.0", gateway="10.0.0.1"),
            Network(id="net1", model="virtio", bridge="vmbr1", mac="AA:BB:CC:DD:EE:00", ip="192.168.1.102", netmask="255.255.255.0", gateway="192.168.1.1"),
        ],
        snapshots=[
            Snapshot(id="snap1", name="pre-update", created="2023-05-10 14:30:00", description="Before system update"),
            Snapshot(id="snap2", name="db-config-change", created="2023-06-15 09:45:00", description="After database configuration change"),
            Snapshot(id="snap3", name="monthly-backup", created="2023-07-01 00:00:00", description="Monthly automated snapshot"),
        ],
        backups=[
            Backup(id="backup1", storage="backup-nfs", created="2023-07-01 01:00:00", size=75840, status="successful"),
            Backup(id="backup2", storage="backup-nfs", created="2023-06-01 01:00:00", size=72560, status="successful"),
            Backup(id="backup3", storage="backup-nfs", created="2023-05-01 01:00:00", size=70240, status="successful"),
        ]
    )
    
    return vm_summary

@router.get(
    '/virtual-machines/{id}/summary',
)
async def get_virtual_machine_summary(id: int):
    pass

@router.get('/virtual-machines/interfaces/create')
async def create_virtual_machines_interfaces():
    # TODO
    pass

@router.get('/virtual-machines/interfaces/ip-address/create')
async def create_virtual_machines_interfaces_ip_address():
    # TODO
    pass

@router.get('/virtual-machines/virtual-disks/create')
async def create_virtual_disks():
    # TODO
    pass

async def create_netbox_backups(backup):
    nb = RawNetBoxSession()
    
    netbox_backups: list = []
    
    try:
        # Get the virtual machine on NetBox by the VM ID.
        vmid = backup.get('vmid', None)
        virtual_machine = None
        if vmid:
            # Get the virtual machine on NetBox by the VM ID.
            # custom_field.proxmox_vm_id = vmid
            virtual_machine = nb.virtualization.virtual_machines.get(cf_proxmox_vm_id=int(vmid))
        
        if virtual_machine:
            verification_state = None
            verification_upid = None
            
            verification = backup.get('verification', None)
                    
            # Get the verification state and upid from the backup.
            if verification:
                verification_state = verification.get('state')
                verification_upid = verification.get('upid')
            
            storage_name = None
            volume_id = backup.get('volid', None)
            if volume_id:
                # Get the storage name from the volume ID.
                # Example: 'local-zfs:vm-102-disk-0' -> 'local-zfs'
                storage_name = volume_id.split(':')[0]
                
            creation_time = backup.get('ctime', None)
            if creation_time:
                # Convert the creation time from a UNIX timestamp to a datetime object.
                creation_time = datetime.fromtimestamp(creation_time).isoformat()
            
            if virtual_machine:
                # Create the backup on NetBox.
                netbox_backup = nb.plugins.proxbox.__getattr__('backups').create(
                    storage=storage_name,
                    virtual_machine=virtual_machine.id,
                    subtype=backup.get('subtype'),
                    creation_time=creation_time,
                    size=backup.get('size'),
                    verification_state=verification_state,
                    verification_upid=verification_upid,
                    volume_id=volume_id,
                    notes=backup.get('notes'),
                    vmid=backup.get('vmid'),
                    format=backup.get('format'),
                )
                
                if netbox_backup:
                    return netbox_backup
    except Exception as error:
        print('Error creating NetBox backup: ', error)
        pass
               
    return None

@router.get('/virtual-machines/backups/create')
async def create_virtual_machine_backups(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    node: Annotated[
        str,
        Query(
            title="Node",
            description="The name of the node to retrieve the storage content for."
        )
    ],
    storage: Annotated[
        str,
        Query(
            title="Storage",
            description="The name of the storage to retrieve the content for."
        )
    ],
    vmid: Annotated[
        str,
        Query(
            title="VM ID",
            description="The ID of the VM to retrieve the content for."
        )
    ] = None
):
    nb = RawNetBoxSession()
    
    netbox_backups: list = []
    
    for proxmox, cluster in zip(pxs, cluster_status):
        print(proxmox, cluster)
        for cluster_node in cluster.node_list:
            if cluster_node.name == node:
                backups = await get_proxmox_node_storage_content(
                    pxs=pxs,
                    cluster_status=cluster_status,
                    node=node,
                    storage=storage,
                    vmid=vmid,
                    content='backup'
                )
                
                backups = [backup for backup in backups if backup.get('content') == 'backup']
                    
                try:
                    return await asyncio.gather(*[create_netbox_backups(backup) for backup in backups])
                    
                except Exception as error:
                    print('Error creating NetBox backups: ', error)
                    pass
                    
                return backups
    
    raise ProxboxException(message="Node or Storage not found.")


@router.get('/virtual-machines/backups/all/create')
async def create_virtual_machine_backups(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
):
    netbox_backups = []

    # Loop through each Proxmox cluster (endpoint).
    for proxmox, cluster in zip(pxs, cluster_status):
        # Get all storage names that have 'backup' in the content.
        storage_list = [
            {
                'storage': storage_dict.get('storage'),
                'nodes': storage_dict.get('nodes', 'all')
            } for storage_dict in proxmox.session.storage.get() 
            if 'backup' in storage_dict.get('content')
        ]
        
        # Loop through each cluster node.
        for cluster_node in cluster.node_list:
            # Loop through each storage.
            for storage in storage_list:
                # If the storage is for all nodes or the current node is in the storage nodes list, get the backups.
                if storage.get('nodes') == 'all' or cluster_node.name in storage.get('nodes'):
                    backups = await get_proxmox_node_storage_content(
                        pxs=pxs,
                        cluster_status=cluster_status,
                        node=cluster_node.name,
                        storage=storage.get('storage'),
                        content='backup'
                    )
                    
                    backups = [backup for backup in backups if backup.get('content') == 'backup']
                    
                    try:
                        current_backups = await asyncio.gather(*[create_netbox_backups(backup) for backup in backups])
                        netbox_backups.extend(current_backups)

                    except Exception as error:
                        print('ERROR: ', error)
                        pass
    
    if netbox_backups:
        return netbox_backups
    else:
        raise ProxboxException(message="No backups found.")