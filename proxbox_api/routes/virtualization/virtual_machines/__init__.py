# FastAPI Imports
from fastapi import APIRouter
from fastapi import WebSocket, Query
from typing import Annotated, Optional

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
from proxbox_api.utils import return_status_html, sync_process # Return Status HTML and Sync Process
from proxbox_api.routes.proxmox import get_vm_config # Get VM Config
from proxbox_api.exception import ProxboxException # Proxbox Exception
from proxbox_api.dependencies import NetBoxSessionDep # NetBox Session
from proxbox_api.logger import logger # Logger

# pynetbox_api Imports
from pynetbox_api.virtualization.virtual_machine import VirtualMachine # Virtual Machine
from pynetbox_api.virtualization.cluster import Cluster # Cluster
from pynetbox_api.dcim.device import Device # Device
from pynetbox_api.dcim.device_role import DeviceRole # Device Role
from pynetbox_api.virtualization.interface import VMInterface # VM Interface
from pynetbox_api.ipam.ip_address import IPAddress # IP Address
from pynetbox_api.cache import global_cache # Global Cache
from proxbox_api.routes.proxmox import get_proxmox_node_storage_content # Get Proxmox Node Storage Content

router = APIRouter()


@router.get('/sync-process/journal-entry/test/create')
async def create_sync_process_journal_entry(netbox_session: NetBoxSessionDep):
    """
    Create a Sync Process, then create a Journal Entry for it.
    """
    
    nb = netbox_session
    start_time = datetime.now().isoformat()
    
    try:
        # Create sync process first
        sync_process = nb.plugins.proxbox.__getattr__('sync-processes').create(
            name=f"journal-entry-test-{datetime.now().isoformat()}",
            sync_type="virtual-machines",
            status="not-started",
            started_at=start_time
        )
        print(f"Created sync process: {sync_process}")
        
        # Try to create journal entries using the string format
        try:
            journal_entry_sync = nb.extras.journal_entries.create({
                'assigned_object_type': 'netbox_proxbox.syncprocess',
                'assigned_object_id': sync_process.id,
                'kind': 'info',
                'comments': 'Journal Entry Test for Sync Process'
            })
            print(f"Created sync process journal entry: {journal_entry_sync}")
            
            journal_entry_sync = nb.extras.journal_entries.create({
                'assigned_object_type': 'netbox_proxbox.syncprocess',
                'assigned_object_id': sync_process.id,
                'kind': 'info',
                'comments': '2 - Journal Entry Test for Sync Process'
            })
            print(f"2 Created sync process journal entry: {journal_entry_sync}")
        except Exception as sync_error:
            print(f"Error creating sync process journal entry: {str(sync_error)}")
            if hasattr(sync_error, 'response'):
                print(f"Response content: {sync_error.response.content}")
        
        try:
            journal_entry_backup = nb.extras.journal_entries.create({
                'assigned_object_type': 'netbox_proxbox.vmbackup',
                'assigned_object_id': 1887,
                'kind': 'info',
                'comments': 'Journal Entry Test for VM Backup'
            })
            print(f"Created VM backup journal entry: {journal_entry_backup}")
            
        except Exception as backup_error:
            print(f"Error creating VM backup journal entry: {str(backup_error)}")
            if hasattr(backup_error, 'response'):
                print(f"Response content: {backup_error.response.content}")
        
    except Exception as error:
        print(f"Detailed error: {str(error)}")
        print(f"Error type: {type(error)}")
        if hasattr(error, 'response'):
            print(f"Response content: {error.response.content}")
        raise
    
    return {
        'status': 'completed',
        'sync_process': sync_process,
        'journal_entries': {
            'sync_process': journal_entry_sync if 'journal_entry_sync' in locals() else None,
            'vm_backup': journal_entry_backup if 'journal_entry_backup' in locals() else None
        }
    }




@router.get('/create-test')
async def create_test():
    '''
    name:  DB-MASTER
    status:  active
    cluster:  1
    device:  29
    vcpus:  4
    memory:  4294
    disk:  34359
    tags:  [2]
    role:  786
    '''
    
    virtual_machine = await asyncio.to_thread(lambda: VirtualMachine(
        name='DB-MASTER',
        status='active',
        cluster=1,
        device=29,
        vcpus=4,
        memory=4294,
        disk=34359,
        tags=[2],
        role=786,
        custom_fields={
            "proxmox_vm_id": 100,
            "proxmox_start_at_boot": True,
            "proxmox_unprivileged_container": False,
            "proxmox_qemu_agent": True,
            "proxmox_search_domain": 'example.com',
        },
    ))
    
    return virtual_machine
    

@router.get('/create')
@sync_process('virtual-machines')
async def create_virtual_machines(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket: WebSocket = None,
    use_css: bool = False,
    use_websocket: bool = False,
    sync_process = None,
):
    '''
    Creates a new virtual machine in Netbox.
    '''
    
    # GET /api/plugins/proxbox/sync-processes/
    nb = netbox_session
    start_time = datetime.now()
    
    journal_messages = []  # Store all journal messages
    total_vms = 0  # Track total VMs processed
    successful_vms = 0  # Track successful VM creations
    failed_vms = 0  # Track failed VM creations
    

    journal_messages.append("## Virtual Machine Sync Process Started")
    journal_messages.append(f"- **Start Time**: {start_time}")
    journal_messages.append("- **Status**: Initializing")
        
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
                'tags': [tag.id],
                'vm_role': True
            },
            'lxc': {
                'name': 'Container (LXC)',
                'slug': 'container-lxc',
                'color': '7fffd4',
                'description': 'Proxmox LXC Container',
                'tags': [tag.id],
                'vm_role': True
            },
            'undefined': {
                'name': 'Unknown',
                'slug': 'unknown',
                'color': '000000',
                'description': 'VM Type not found. Neither QEMU nor LXC.',
                'tags': [tag.id],
                'vm_role': True
            }
        }
        
        vm_type = resource.get('type', 'unknown')
        vm_config = await get_vm_config(
            pxs=pxs,
            cluster_status=cluster_status,
            node=resource.get("node"),
            type=vm_type,
            vmid=resource.get("vmid")
        )
        
        if vm_config is None:
            vm_config = {}
            
        start_at_boot = True if vm_config.get('onboot', 0) == 1 else False
        qemu_agent = True if vm_config.get('agent', 0) == 1 else False
        unprivileged_container = True if vm_config.get('unprivileged', 0) == 1 else False
        search_domain = vm_config.get('searchdomain', None)
        
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
            cluster = await asyncio.to_thread(lambda: Cluster(name=cluster_name, tags=[getattr(tag, 'id')]))
            device = await asyncio.to_thread(lambda: Device(name=resource.get('node'), tags=[getattr(tag, 'id')]))
            role = await asyncio.to_thread(lambda: DeviceRole(**vm_role_mapping.get(vm_type, {})))
            
            print(f"Cluster: {cluster} / {cluster.id}")
            print(f"Device: {device} / {device.id}")
            print(f"Role: {role} / {role.id}")
            print('\n')            
            
        except Exception as error:
            raise ProxboxException(
                message="Error creating Virtual Machine dependent objects (cluster, device, tag and role)",
                python_exception=f"Error: {str(error)}"
            )
            
        #try:
        print('name: ', resource.get('name'))
        print('status: ', VirtualMachine.status_field.get(resource.get('status'), 'active'))
        print('cluster: ', getattr(cluster, 'id'))
        print('device: ', getattr(device, 'id'))
        print('vcpus: ', int(resource.get("maxcpu", 0)))
        print('memory: ', int(resource.get("maxmem")) // 1000000)
        print('disk: ', int(resource.get("maxdisk", 0)) // 1000000)
        print('tags: ', [getattr(tag, 'id')])
        print('role: ', getattr(role, 'id'))
        
        virtual_machine = await asyncio.to_thread(lambda: VirtualMachine(
            name=resource.get('name'),
            status=VirtualMachine.status_field.get(resource.get('status'), 'active'),
            cluster=getattr(cluster, 'id'),
            device=getattr(device, 'id'),
            vcpus=int(resource.get("maxcpu", 0)),
            memory=int(resource.get("maxmem")) // 1000000,
            disk=int(resource.get("maxdisk", 0)) // 1000000,
            tags=[getattr(tag, 'id')],
            role=getattr(role, 'id'),
            custom_fields={
                "proxmox_vm_id": resource.get('vmid'),
                "proxmox_start_at_boot": start_at_boot,
                "proxmox_unprivileged_container": unprivileged_container,
                "proxmox_qemu_agent": qemu_agent,
                "proxmox_search_domain": search_domain,
            },
        ))
        
        print(f"Virtual Machine: {virtual_machine} / {virtual_machine.id}")
        print('\n')
        
        '''
        except ProxboxException:
            raise
        except Exception as error:
            raise ProxboxException(
                message="Error creating Virtual Machine in Netbox",
                python_exception=f"Error: {str(error)}"
            )
        '''
            
        if type(virtual_machine) != dict:
            virtual_machine = virtual_machine.dict()
            
        # Create VM interfaces
        netbox_vm_interfaces = []
        if virtual_machine and vm_config:
            vm_networks = []
            network_id = 0
            while True:
                network_name = f'net{network_id}'
                vm_network_info = vm_config.get(network_name, None)
                if vm_network_info is not None:
                    net_fields = vm_network_info.split(',')
                    network_dict = dict([field.split('=') for field in net_fields])
                    vm_networks.append({network_name: network_dict})
                    network_id += 1
                else:
                    break
            
            if vm_networks:
                for network in vm_networks:
                    for interface_name, value in network.items():
                        bridge_name = value.get('bridge', None)
                        bridge = {}
                        if bridge_name:
                            bridge = VMInterface(
                                name=bridge_name,
                                virtual_machine=virtual_machine.get('id'),
                                type='bridge',
                                description=f'Bridge interface of Device {resource.get("node")}.',
                                tags=[tag.id]
                            )
                            
                        if type(bridge) != dict:
                            bridge = bridge.dict()
                            
                        vm_interface = await asyncio.to_thread(lambda: VMInterface(
                            virtual_machine=virtual_machine.get('id'),
                            name=value.get('name', interface_name),
                            enabled=True,
                            bridge=bridge.get('id', None),
                            mac_address=value.get('virtio', value.get('hwaddr', None)),
                            tags=[tag.id]
                        ))
                        
                        if type(vm_interface) != dict:
                            vm_interface = vm_interface.dict()
                            
                        netbox_vm_interfaces.append(vm_interface)
                        
                        interface_ip = value.get('ip', None)
                        if interface_ip and interface_ip != 'dhcp':
                            IPAddress(
                                address=interface_ip,
                                assigned_object_type='virtualization.vminterface',
                                assigned_object_id=vm_interface.get('id'),
                                status='active',
                                tags=[tag.id],
                            )
        
        return virtual_machine
    
    async def _create_cluster_vms(cluster: dict) -> list:
        """
        Create virtual machines for a cluster.
        
        Args:
            cluster: A dictionary containing cluster information.
            
        Returns:
            A list of virtual machine creation results.
        """
        
        tasks = []  # Collect coroutines
        for cluster_name, resources in cluster.items():
            for resource in resources:
                if resource.get('type') in ('qemu', 'lxc'):
                    tasks.append(create_vm_task(cluster_name, resource))

        return await asyncio.gather(*tasks, return_exceptions=True)  # Gather coroutines
    
    try:
        journal_messages.append("\n## Virtual Machine Discovery")
        
        # Process each cluster
        for cluster in cluster_resources:
            cluster_name = list(cluster.keys())[0]
            resources = cluster[cluster_name]
            vm_count = len([r for r in resources if r.get('type') in ('qemu', 'lxc')])
            
            journal_messages += [
                f"\n### Processing Cluster: {cluster_name}",
                f"- Found {vm_count} virtual machines"
            ]
            
            total_vms += vm_count
        
        journal_messages += [
            "\n## Virtual Machine Processing",
            f"- Total VMs to process: {total_vms}"
        ]
        
        # Return the created virtual machines.
        result_list = await asyncio.gather(*[_create_cluster_vms(cluster) for cluster in cluster_resources], return_exceptions=True)
        
        logger.info(f"VM Creation Result list: {result_list}")
        for result in result_list[0]:
            if isinstance(result, Exception):
                print('python_exception: ', result.python_exception)
                print('str(result): ', str(result))
                print('')

            
        
        # Flatten the nested results and process them
        flattened_results = []
        for cluster_results in result_list:
            if isinstance(cluster_results, Exception):
                failed_vms += 1
                journal_messages.append(f"- ❌ Failed to process cluster: {str(cluster_results)}")
            else:
                # cluster_results is a list of VM creation results
                for vm_result in cluster_results:
                    if isinstance(vm_result, Exception):
                        failed_vms += 1
                        journal_messages.append(f"- ❌ Failed to create VM: {str(vm_result)}")
                    else:
                        successful_vms += 1
                        journal_messages.append(f"- ✅ Successfully created VM: {vm_result.get('name')} (ID: {vm_result.get('id')})")
                        flattened_results.append(vm_result)
        
        # Send end message to websocket
        if all([use_websocket, websocket]):
            await websocket.send_json({'object': 'virtual_machine', 'end': True})

        # Clear cache after creating virtual machines
        global_cache.clear_cache()
        
    except Exception as error:
        error_msg = f"Error during VM sync: {str(error)}"
        journal_messages.append(f"\n### ❌ Error\n{error_msg}")
        raise ProxboxException(message=error_msg)
    
    finally:
        # Add final summary
        journal_messages += [
            "\n## Process Summary",
            f"- **Status**: {getattr(sync_process, 'status', 'unknown')}",
            f"- **Runtime**: {getattr(sync_process, 'runtime', 'unknown')} seconds",
            f"- **End Time**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- **Total VMs Processed**: {total_vms}",
            f"- **Successfully Created**: {successful_vms}",
            f"- **Failed**: {failed_vms}"
        ]
        
        
        journal_entry = nb.extras.journal_entries.create({
            'assigned_object_type': 'netbox_proxbox.syncprocess',
            'assigned_object_id': sync_process.id,
            'kind': 'info',
            'comments': '\n'.join(journal_messages)
        })
        
        if not journal_entry:
            print("Warning: Journal entry creation returned None")

    return flattened_results

@router.get(
    '/',
    response_model=VirtualMachine.SchemaList,
    response_model_exclude_none=True,
    response_model_exclude_unset=True
)
async def get_virtual_machines():
    virtual_machine = VirtualMachine()
    return virtual_machine.all()

@router.get(
    '/{id}',
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
    '/summary/example',
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
    '/{id}/summary',
)
async def get_virtual_machine_summary(id: int):
    pass

@router.get('/interfaces/create')
async def create_virtual_machines_interfaces():
    # TODO
    pass

@router.get('/interfaces/ip-address/create')
async def create_virtual_machines_interfaces_ip_address():
    # TODO
    pass

@router.get('/virtual-disks/create')
async def create_virtual_disks():
    # TODO
    pass

async def create_netbox_backups(backup, netbox_session: NetBoxSessionDep):
    nb = netbox_session
    try:
        # Get the virtual machine on NetBox by the VM ID.
        vmid = backup.get('vmid', None)
        if not vmid:
            return None
            
        # Get the virtual machine on NetBox by the VM ID.
        # Use a cached session to avoid repeated API calls
        virtual_machine = await asyncio.to_thread(
            lambda: nb.virtualization.virtual_machines.get(cf_proxmox_vm_id=int(vmid))
        )
        
        if not virtual_machine:
            return None
            
        # Process verification data
        verification = backup.get('verification', {})
        verification_state = verification.get('state')
        verification_upid = verification.get('upid')
        
        # Process storage and volume data
        volume_id = backup.get('volid', None)
        storage_name = volume_id.split(':')[0] if volume_id else None
        
        # Process creation time
        creation_time = None
        ctime = backup.get('ctime', None)
        if ctime:
            creation_time = datetime.fromtimestamp(ctime).isoformat()
        
        try:
            # Create the backup on NetBox using a cached session
            netbox_backup = await asyncio.to_thread(
                lambda: nb.plugins.proxbox.__getattr__('backups').create(
                    storage=storage_name,
                    virtual_machine=virtual_machine.id,
                    subtype=backup.get('subtype'),
                    creation_time=creation_time,
                    size=backup.get('size'),
                    verification_state=verification_state,
                    verification_upid=verification_upid,
                    volume_id=volume_id,
                    notes=backup.get('notes'),
                    vmid=vmid,
                    format=backup.get('format'),
                )
            )
            
            # Create a journal entry for the backup
            journal_entry = nb.extras.journal_entries.create({
                'assigned_object_type': 'netbox_proxbox.vmbackup',
                'assigned_object_id': netbox_backup.id,
                'kind': 'info',
                'comments': f'Backup created for VM {vmid} in storage {storage_name}'
            })
            
            return netbox_backup
            
        except Exception as error:
            # Check if the error is due to a duplicate backup
            if 'already exists' in str(error):
                # Return a special object indicating this is a duplicate
                return {
                    'status': 'duplicate',
                    'virtual_machine': virtual_machine,
                    'storage': storage_name,
                    'volume_id': volume_id,
                    'creation_time': creation_time,
                    'vmid': vmid
                }
            else:
                # For other errors, raise the exception
                raise
        
    except Exception as error:
        print(f'Error creating NetBox backup for VM {vmid}: {error}')
        return None

async def process_backups_batch(backup_tasks: list, batch_size: int = 10) -> list:
    """
    Process a list of backup tasks in batches to avoid overwhelming the API.
    
    Args:
        backup_tasks: List of backup creation tasks
        batch_size: Number of tasks to process in each batch
        
    Returns:
        List of successfully created backups
    """
    results = []
    for i in range(0, len(backup_tasks), batch_size):
        batch = backup_tasks[i:i + batch_size]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)
        results.extend([r for r in batch_results if r is not None])
    return results

async def get_node_backups(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    node: str,
    storage: str,
    netbox_session: NetBoxSessionDep,
    vmid: Optional[str] = None
) -> list:
    nb = netbox_session
    """
    Get backups for a specific node and storage.
    
    Args:
        pxs: Proxmox sessions
        cluster_status: Cluster status information
        node: Node name
        storage: Storage name
        vmid: Optional VM ID to filter backups
        
    Returns:
        List of backup tasks
    """
    for proxmox, cluster in zip(pxs, cluster_status):
        if cluster and cluster.node_list:
            for cluster_node in cluster.node_list:
                if cluster_node.name == node:
                    try:
                        backups = await get_proxmox_node_storage_content(
                            pxs=pxs,
                            cluster_status=cluster_status,
                            node=node,
                            storage=storage,
                            vmid=vmid,
                            content='backup'
                        )
                        
                        return [
                            create_netbox_backups(backup, nb) 
                            for backup in backups 
                            if backup.get('content') == 'backup'
                        ]
                    except Exception as error:
                        print(f'Error getting backups for node {node}: {error}')
                        continue
    return []

@router.get('/backups/create')
async def create_virtual_machine_backups(
    netbox_session: NetBoxSessionDep,
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
        Optional[str],
        Query(
            title="VM ID",
            description="The ID of the VM to retrieve the content for."
        )
    ] = None
):
    backup_tasks = await get_node_backups(pxs, cluster_status, node, storage, netbox_session=netbox_session, vmid=vmid)
    if not backup_tasks:
        raise ProxboxException(message="Node or Storage not found.")
    
    return await process_backups_batch(backup_tasks)

@router.get('/backups/all/create')
async def create_all_virtual_machine_backups(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    delete_nonexistent_backup: Annotated[
        bool,
        Query(
            title="Delete Nonexistent Backup",
            description="If true, deletes backups that exist in NetBox but not in Proxmox."
        )
    ] = False
):
    nb = netbox_session
    start_time = datetime.now()
    sync_process = None
    results = []
    journal_messages = []  # Store all journal messages
    duplicate_count = 0  # Track number of duplicate backups
    deleted_count = 0  # Track number of deleted backups
    
    try:
        # Create sync process
        sync_process = nb.plugins.proxbox.__getattr__('sync-processes').create(
            name=f"sync-virtual-machines-backups-{start_time}",
            sync_type="vm-backups",
            status="not-started",
            started_at=str(start_time),
            completed_at=None,
            runtime=None,
            tags=[tag.id],
        )
        
        journal_messages.append("## Backup Sync Process Started")
        journal_messages.append(f"- **Start Time**: {start_time}")
        journal_messages.append("- **Status**: Initializing")
        
    except Exception as error:
        error_msg = f"Error creating sync process: {str(error)}"
        journal_messages.append(f"### ❌ Error\n{error_msg}")
        raise ProxboxException(message=error_msg)
    
    try:
        sync_process.status = "syncing"
        sync_process.save()
        
        journal_messages.append("\n## Backup Discovery")
        all_backup_tasks = []
        proxmox_backups = set()  # Store all Proxmox backup identifiers
        
        # Process each Proxmox cluster
        for proxmox, cluster in zip(pxs, cluster_status):
            # Get all storage names that have 'backup' in the content
            storage_list = [
                {
                    'storage': storage_dict.get('storage'),
                    'nodes': storage_dict.get('nodes', 'all')
                } for storage_dict in proxmox.session.storage.get() 
                if 'backup' in storage_dict.get('content')
            ]
            
            journal_messages.append(f"\n### Processing Cluster: {cluster.name}")
            journal_messages.append(f"- Found {len(storage_list)} backup storages")
            
            # Process each cluster node
            if cluster and cluster.node_list:
                for cluster_node in cluster.node_list:
                    # Process each storage
                    for storage in storage_list:
                        if storage.get('nodes') == 'all' or cluster_node.name in storage.get('nodes', []):
                            try:
                                node_backup_tasks = await get_node_backups(
                                    pxs=pxs,
                                    cluster_status=cluster_status,
                                    node=cluster_node.name,
                                    storage=storage.get('storage'),
                                    nb=nb
                                )
                                all_backup_tasks.extend(node_backup_tasks)
                                
                                # Add backup identifiers to the set
                                for backup in node_backup_tasks:
                                    if isinstance(backup, dict) and backup.get('volume_id'):
                                        proxmox_backups.add(backup.get('volume_id'))
                                
                                journal_messages.append(f"- Node `{cluster_node.name}` in storage `{storage.get('storage')}`: Found {len(node_backup_tasks)} backups")
                                
                            except Exception as error:
                                error_msg = f"Error processing backups for node {cluster_node.name} and storage {storage.get('storage')}: {str(error)}"
                                journal_messages.append(f"  - ❌ {error_msg}")
                                continue
        
        if not all_backup_tasks:
            error_msg = "No backups found to process"
            journal_messages.append(f"\n### ⚠️ Warning\n{error_msg}")
            raise ProxboxException(message=error_msg)
        
        journal_messages.append(f"\n## Backup Processing")
        journal_messages.append(f"- Total backups to process: {len(all_backup_tasks)}")
        
        # Process all backups in batches
        results = await process_backups_batch(all_backup_tasks)
        
        # Count successful and duplicate backups
        successful_backups = [r for r in results if isinstance(r, dict) and r.get('status') != 'duplicate']
        duplicate_backups = [r for r in results if isinstance(r, dict) and r.get('status') == 'duplicate']
        duplicate_count = len(duplicate_backups)
        
        journal_messages.append(f"- Successfully created: {len(successful_backups)} backups")
        if duplicate_count > 0:
            journal_messages.append(f"- Skipped {duplicate_count} duplicate backups")
            # Add details about duplicate backups
            journal_messages.append("\n### Duplicate Backups")
            for dup in duplicate_backups:
                journal_messages.append(f"- VM ID {dup.get('vmid')} in storage {dup.get('storage')} (created at {dup.get('creation_time')})")
        
        # Handle deletion of nonexistent backups if requested
        if delete_nonexistent_backup:
            journal_messages.append("\n## Deleting Nonexistent Backups")
            try:
                # Get all backups from NetBox
                netbox_backups = nb.plugins.proxbox.__getattr__('backups').all()
                
                for backup in netbox_backups:
                    print(f"Backup: {backup} | Backup Volume ID: {backup.volume_id} | Proxmox Backups: {proxmox_backups}")
                    if backup.volume_id not in proxmox_backups:
                        try:
                            # Delete the backup
                            backup.delete()
                            deleted_count += 1
                            journal_messages.append(f"- Deleted backup for VM ID {backup.vmid} in storage {backup.storage} (volume: {backup.volume_id})")
                        except Exception as error:
                            journal_messages.append(f"- ❌ Failed to delete backup for VM ID {backup.vmid}: {str(error)}")
                
                if deleted_count > 0:
                    journal_messages.append(f"\nTotal backups deleted: {deleted_count}")
                else:
                    journal_messages.append("\nNo backups needed to be deleted")
                    
            except Exception as error:
                error_msg = f"Error during backup deletion: {str(error)}"
                journal_messages.append(f"\n### ❌ Error\n{error_msg}")
                # Don't raise the exception as this is not critical for the sync process
        
    except Exception as error:
        error_msg = f"Error during backup sync: {str(error)}"
        journal_messages.append(f"\n### ❌ Error\n{error_msg}")
        if sync_process:
            sync_process.status = "not-started"
            sync_process.completed_at = str(datetime.now())
            sync_process.runtime = float((datetime.now() - start_time).total_seconds())
            sync_process.save()
        raise ProxboxException(message=error_msg)
    
    finally:
        # Always update sync process status
        if sync_process:
            end_time = datetime.now()
            sync_process.status = "completed" if results else "not-started"
            sync_process.completed_at = str(end_time)
            sync_process.runtime = float((end_time - start_time).total_seconds())
            sync_process.save()
            
            # Add final summary
            journal_messages.append(f"\n## Process Summary")
            journal_messages.append(f"- **Status**: {sync_process.status}")
            journal_messages.append(f"- **Runtime**: {sync_process.runtime} seconds")
            journal_messages.append(f"- **End Time**: {end_time}")
            journal_messages.append(f"- **Total Backups Processed**: {len(results)}")
            journal_messages.append(f"- **New Backups Created**: {len(results) - duplicate_count}")
            journal_messages.append(f"- **Duplicate Backups Skipped**: {duplicate_count}")
            if delete_nonexistent_backup:
                journal_messages.append(f"- **Backups Deleted**: {deleted_count}")
            
            journal_entry = nb.extras.journal_entries.create({
                'assigned_object_type': 'netbox_proxbox.syncprocess',
                'assigned_object_id': sync_process.id,
                'kind': 'info',
                'comments': '\n'.join(journal_messages)
            })
            
            if not journal_entry:
                print("Warning: Journal entry creation returned None")
    print('Syncing Backups Finished.')
    return results