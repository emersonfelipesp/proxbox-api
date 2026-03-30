"""Virtual machine sync routes and backup workflows."""

# FastAPI Imports
import asyncio
import os
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from proxbox_api.cache import global_cache
from proxbox_api.dependencies import (
    NetBoxSessionDep,  # NetBox Session
    ProxboxTagDep,  # Proxbox Tag
)
from proxbox_api.exception import ProxboxException  # Proxbox Exception
from proxbox_api.logger import logger  # Logger

# NetBox compatibility wrappers
from proxbox_api.netbox_compat import (
    VirtualMachine,
)
from proxbox_api.netbox_rest import (
    rest_create,
    rest_create_async,
    rest_list,
    rest_list_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxBackupSyncState,
    NetBoxDeviceRoleSyncState,
    NetBoxIpAddressSyncState,
    NetBoxVirtualDiskSyncState,
    NetBoxVirtualMachineCreateBody,
    NetBoxVirtualMachineInterfaceSyncState,
    ProxmoxVmConfigInput,
)
from proxbox_api.routes.extras import CreateCustomFieldsDep  # Create Custom Fields
from proxbox_api.routes.proxmox import (
    get_proxmox_node_storage_content,
    get_vm_config,  # Get VM Config
)  # Get Proxmox Node Storage Content
from proxbox_api.routes.proxmox.cluster import (
    ClusterResourcesDep,
    ClusterStatusDep,
)  # Cluster Status and Resources
from proxbox_api.schemas.virtualization import (  # Schemas
    CPU,
    Backup,
    Disk,
    Memory,
    Network,
    Snapshot,
    VirtualMachineSummary,
)
from proxbox_api.services.sync.devices import (
    _ensure_cluster,
    _ensure_cluster_type,
    _ensure_device,
    _ensure_device_type,
    _ensure_manufacturer,
    _ensure_site,
)
from proxbox_api.services.sync.devices import (
    _ensure_device_role as _ensure_proxmox_node_role,
)
from proxbox_api.services.sync.snapshots import (
    create_virtual_machine_snapshots as sync_snapshots,
)
from proxbox_api.services.sync.virtual_disks import (
    create_virtual_disks as sync_virtual_disks,
)
from proxbox_api.services.sync.virtual_machines import (
    build_netbox_virtual_machine_payload,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep  # Sessions
from proxbox_api.utils import (
    return_status_html,
    sync_process,
)  # Return Status HTML and Sync Process
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

router = APIRouter()


def _resolve_vm_sync_concurrency() -> int:
    raw_value = os.environ.get("PROXBOX_VM_SYNC_MAX_CONCURRENCY", "").strip()
    if not raw_value:
        return 4
    try:
        value = int(raw_value)
    except ValueError:
        return 4
    return max(1, value)


@router.get("/sync-process/journal-entry/test/create")
async def create_sync_process_journal_entry(netbox_session: NetBoxSessionDep):
    """
    Create a Sync Process, then create a Journal Entry for it.
    """

    nb = netbox_session
    start_time = datetime.now().isoformat()
    journal_entry_sync = None
    journal_entry_backup = None

    try:
        # Create sync process first
        sync_process = rest_create(
            nb,
            "/api/plugins/proxbox/sync-processes/",
            {
                "name": f"journal-entry-test-{datetime.now().isoformat()}",
                "sync_type": "virtual-machines",
                "status": "not-started",
                "started_at": start_time,
            },
        )
        print(f"Created sync process: {sync_process}")

        # Try to create journal entries using the string format
        try:
            journal_entry_sync = await rest_create_async(
                nb,
                "/api/extras/journal-entries/",
                {
                    "assigned_object_type": "netbox_proxbox.syncprocess",
                    "assigned_object_id": sync_process.id,
                    "kind": "info",
                    "comments": "Journal Entry Test for Sync Process",
                },
            )
            print(f"Created sync process journal entry: {journal_entry_sync}")

            journal_entry_sync = await rest_create_async(
                nb,
                "/api/extras/journal-entries/",
                {
                    "assigned_object_type": "netbox_proxbox.syncprocess",
                    "assigned_object_id": sync_process.id,
                    "kind": "info",
                    "comments": "2 - Journal Entry Test for Sync Process",
                },
            )
            print(f"2 Created sync process journal entry: {journal_entry_sync}")
        except Exception as sync_error:
            print(f"Error creating sync process journal entry: {str(sync_error)}")
            if hasattr(sync_error, "response"):
                print(f"Response content: {sync_error.response.content}")

        try:
            journal_entry_backup = await rest_create_async(
                nb,
                "/api/extras/journal-entries/",
                {
                    "assigned_object_type": "netbox_proxbox.vmbackup",
                    "assigned_object_id": 1887,
                    "kind": "info",
                    "comments": "Journal Entry Test for VM Backup",
                },
            )
            print(f"Created VM backup journal entry: {journal_entry_backup}")

        except Exception as backup_error:
            print(f"Error creating VM backup journal entry: {str(backup_error)}")
            if hasattr(backup_error, "response"):
                print(f"Response content: {backup_error.response.content}")

    except Exception as error:
        print(f"Detailed error: {str(error)}")
        print(f"Error type: {type(error)}")
        if hasattr(error, "response"):
            print(f"Response content: {error.response.content}")
        raise

    return {
        "status": "completed",
        "sync_process": sync_process,
        "journal_entries": {
            "sync_process": journal_entry_sync if "journal_entry_sync" in locals() else None,
            "vm_backup": journal_entry_backup if "journal_entry_backup" in locals() else None,
        },
    }


@router.get("/create-test")
async def create_test():
    """
    name:  DB-MASTER
    status:  active
    cluster:  1
    device:  29
    vcpus:  4
    memory:  4294
    disk:  34359
    tags:  [2]
    role:  786
    """

    virtual_machine = await asyncio.to_thread(
        lambda: VirtualMachine(
            name="DB-MASTER",
            status="active",
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
                "proxmox_search_domain": "example.com",
            },
        )
    )

    return virtual_machine


@router.get("/create")
@sync_process("virtual-machines")
async def create_virtual_machines(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_css: bool = False,
    use_websocket: bool = False,
    sync_process=None,
):
    """
    Creates a new virtual machine in Netbox.
    """

    # GET /api/plugins/proxbox/sync-processes/
    nb = netbox_session
    start_time = datetime.now()

    journal_messages = []  # Store all journal messages
    total_vms = 0  # Track total VMs processed
    successful_vms = 0  # Track successful VM creations
    failed_vms = 0  # Track failed VM creations
    tag_id = int(getattr(tag, "id", 0) or 0)
    tag_refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    tag_refs = [tag_ref for tag_ref in tag_refs if tag_ref.get("name") and tag_ref.get("slug")]
    flattened_results = []

    journal_messages.append("## Virtual Machine Sync Process Started")
    journal_messages.append(f"- **Start Time**: {start_time}")
    journal_messages.append("- **Status**: Initializing")

    async def create_vm_task(cluster_name, resource):
        undefined_html = return_status_html("undefined", use_css)

        websocket_vm_json: dict = {
            "sync_status": return_status_html("syncing", use_css),
            "name": undefined_html,
            "netbox_id": undefined_html,
            "status": undefined_html,
            "cluster": undefined_html,
            "device": undefined_html,
            "role": undefined_html,
            "vcpus": undefined_html,
            "memory": undefined_html,
            "disk": undefined_html,
            "vm_interfaces": undefined_html,
        }

        vm_role_mapping: dict = {
            "qemu": {
                "name": "Virtual Machine (QEMU)",
                "slug": "virtual-machine-qemu",
                "color": "00ffff",
                "description": "Proxmox Virtual Machine",
                "tags": [tag_id],
                "vm_role": True,
            },
            "lxc": {
                "name": "Container (LXC)",
                "slug": "container-lxc",
                "color": "7fffd4",
                "description": "Proxmox LXC Container",
                "tags": [tag_id],
                "vm_role": True,
            },
            "undefined": {
                "name": "Unknown",
                "slug": "unknown",
                "color": "000000",
                "description": "VM Type not found. Neither QEMU nor LXC.",
                "tags": [tag_id],
                "vm_role": True,
            },
        }

        vm_type = resource.get("type", "unknown")
        vm_config = await get_vm_config(
            pxs=pxs,
            cluster_status=cluster_status,
            node=resource.get("node"),
            type=vm_type,
            vmid=resource.get("vmid"),
        )

        if vm_config is None:
            vm_config = {}

        initial_vm_json = websocket_vm_json | {
            "completed": False,
            "rowid": str(resource.get("name")),
            "name": str(resource.get("name")),
            "cluster": str(cluster_name),
            "device": str(resource.get("node")),
        }

        if all([use_websocket, websocket]):
            await websocket.send_json(
                {"object": "virtual_machine", "type": "create", "data": initial_vm_json}
            )

        try:
            cluster_mode = next(
                (
                    cluster_state.mode
                    for cluster_state in cluster_status
                    if getattr(cluster_state, "name", None) == cluster_name
                ),
                "cluster",
            )
            cluster_type = await _ensure_cluster_type(
                nb,
                mode=cluster_mode,
                tag_refs=tag_refs,
            )
            cluster = await _ensure_cluster(
                nb,
                cluster_name=cluster_name,
                cluster_type_id=getattr(cluster_type, "id", None),
                mode=cluster_mode,
                tag_refs=tag_refs,
            )
            manufacturer = await _ensure_manufacturer(nb, tag_refs=tag_refs)
            device_type = await _ensure_device_type(
                nb,
                manufacturer_id=getattr(manufacturer, "id", None),
                tag_refs=tag_refs,
            )
            device_role = await _ensure_proxmox_node_role(nb, tag_refs=tag_refs)
            site = await _ensure_site(nb, cluster_name=cluster_name, tag_refs=tag_refs)
            device = await _ensure_device(
                nb,
                device_name=resource.get("node"),
                cluster_id=getattr(cluster, "id", None),
                device_type_id=getattr(device_type, "id", None),
                role_id=getattr(device_role, "id", None),
                site_id=getattr(site, "id", None),
                tag_refs=tag_refs,
            )
            role = await rest_reconcile_async(
                nb,
                "/api/dcim/device-roles/",
                lookup={"slug": vm_role_mapping.get(vm_type, {}).get("slug")},
                payload={
                    **vm_role_mapping.get(vm_type, {}),
                    "tags": tag_refs,
                },
                schema=NetBoxDeviceRoleSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "color": record.get("color"),
                    "description": record.get("description"),
                    "vm_role": record.get("vm_role"),
                    "tags": record.get("tags"),
                },
            )

            print(f"Cluster: {cluster} / {cluster.id}")
            print(f"Device: {device} / {device.id}")
            print(f"Role: {role} / {role.id}")
            print("\n")

        except Exception as error:
            raise ProxboxException(
                message="Error creating Virtual Machine dependent objects (cluster, device, tag and role)",
                python_exception=f"Error: {str(error)}",
            )

        # try:
        netbox_vm_payload = build_netbox_virtual_machine_payload(
            proxmox_resource=resource,
            proxmox_config=vm_config,
            cluster_id=int(getattr(cluster, "id", 0) or 0),
            device_id=int(getattr(device, "id", 0) or 0),
            role_id=int(getattr(role, "id", 0) or 0),
            tag_ids=[int(getattr(tag, "id", 0) or 0)],
        )

        virtual_machine = await rest_reconcile_async(
            nb,
            "/api/virtualization/virtual-machines/",
            lookup={
                "cf_proxmox_vm_id": int(resource.get("vmid")),
                "cluster_id": int(getattr(cluster, "id", 0) or 0),
            },
            payload=netbox_vm_payload,
            schema=NetBoxVirtualMachineCreateBody,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "status": record.get("status"),
                "cluster": record.get("cluster"),
                "device": record.get("device"),
                "role": record.get("role"),
                "vcpus": record.get("vcpus"),
                "memory": record.get("memory"),
                "disk": record.get("disk"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
                "description": record.get("description"),
            },
        )

        print(f"Virtual Machine: {virtual_machine} / {virtual_machine.id}")
        print("\n")

        """
        except ProxboxException:
            raise
        except Exception as error:
            raise ProxboxException(
                message="Error creating Virtual Machine in Netbox",
                python_exception=f"Error: {str(error)}"
            )
        """

        if not isinstance(virtual_machine, dict):
            virtual_machine = virtual_machine.dict()

        # Create VM interfaces
        netbox_vm_interfaces = []
        if virtual_machine and vm_config:
            vm_networks = []
            network_id = 0
            while True:
                network_name = f"net{network_id}"
                vm_network_info = vm_config.get(network_name, None)
                if vm_network_info is not None:
                    net_fields = vm_network_info.split(",")
                    network_dict = dict([field.split("=") for field in net_fields])
                    vm_networks.append({network_name: network_dict})
                    network_id += 1
                else:
                    break

            if vm_networks:
                for network in vm_networks:
                    for interface_name, value in network.items():
                        bridge_name = value.get("bridge", None)
                        bridge = {}
                        if bridge_name:
                            bridge = await rest_reconcile_async(
                                nb,
                                "/api/virtualization/interfaces/",
                                lookup={
                                    "virtual_machine_id": virtual_machine.get("id"),
                                    "name": bridge_name,
                                },
                                payload={
                                    "name": bridge_name,
                                    "virtual_machine": virtual_machine.get("id"),
                                    "type": "bridge",
                                    "description": f"Bridge interface of Device {resource.get('node')}.",
                                    "tags": tag_refs,
                                },
                                schema=NetBoxVirtualMachineInterfaceSyncState,
                                current_normalizer=lambda record: {
                                    "name": record.get("name"),
                                    "virtual_machine": record.get("virtual_machine"),
                                    "type": record.get("type"),
                                    "description": record.get("description"),
                                    "bridge": record.get("bridge"),
                                    "enabled": record.get("enabled"),
                                    "mac_address": record.get("mac_address"),
                                    "tags": record.get("tags"),
                                },
                            )

                        if not isinstance(bridge, dict):
                            bridge = bridge.dict()

                        vm_interface = await rest_reconcile_async(
                            nb,
                            "/api/virtualization/interfaces/",
                            lookup={
                                "virtual_machine_id": virtual_machine.get("id"),
                                "name": value.get("name", interface_name),
                            },
                            payload={
                                "virtual_machine": virtual_machine.get("id"),
                                "name": value.get("name", interface_name),
                                "enabled": True,
                                "bridge": bridge.get("id", None),
                                "mac_address": value.get("virtio", value.get("hwaddr", None)),
                                "tags": tag_refs,
                            },
                            schema=NetBoxVirtualMachineInterfaceSyncState,
                            current_normalizer=lambda record: {
                                "name": record.get("name"),
                                "virtual_machine": record.get("virtual_machine"),
                                "enabled": record.get("enabled"),
                                "bridge": record.get("bridge"),
                                "mac_address": record.get("mac_address"),
                                "type": record.get("type"),
                                "description": record.get("description"),
                                "tags": record.get("tags"),
                            },
                        )

                        if not isinstance(vm_interface, dict):
                            vm_interface = vm_interface.dict()

                        netbox_vm_interfaces.append(vm_interface)

                        interface_ip = value.get("ip", None)
                        if interface_ip and interface_ip != "dhcp":
                            await rest_reconcile_async(
                                nb,
                                "/api/ipam/ip-addresses/",
                                lookup={"address": interface_ip},
                                payload={
                                    "address": interface_ip,
                                    "assigned_object_type": "virtualization.vminterface",
                                    "assigned_object_id": vm_interface.get("id"),
                                    "status": "active",
                                    "tags": tag_refs,
                                },
                                schema=NetBoxIpAddressSyncState,
                                current_normalizer=lambda record: {
                                    "address": record.get("address"),
                                    "assigned_object_type": record.get("assigned_object_type"),
                                    "assigned_object_id": record.get("assigned_object_id"),
                                    "status": record.get("status"),
                                    "tags": record.get("tags"),
                                },
                            )

                        vm_config_obj = ProxmoxVmConfigInput.model_validate(vm_config)
                        for disk_entry in vm_config_obj.disks:
                            await rest_reconcile_async(
                                nb,
                                "/api/virtualization/virtual-disks/",
                                lookup={
                                    "virtual_machine_id": virtual_machine.get("id"),
                                    "name": disk_entry.name,
                                },
                                payload={
                                    "virtual_machine": virtual_machine.get("id"),
                                    "name": disk_entry.name,
                                    "size": disk_entry.size,
                                    "description": disk_entry.description,
                                    "tags": tag_refs,
                                },
                                schema=NetBoxVirtualDiskSyncState,
                                current_normalizer=lambda record: {
                                    "virtual_machine": record.get("virtual_machine"),
                                    "name": record.get("name"),
                                    "size": record.get("size"),
                                    "description": record.get("description"),
                                    "tags": record.get("tags"),
                                },
                            )

        return virtual_machine

    max_concurrency = _resolve_vm_sync_concurrency()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_vm_task(cluster_name: str, resource: dict):
        async with semaphore:
            return await create_vm_task(cluster_name, resource)

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
                if resource.get("type") in ("qemu", "lxc"):
                    tasks.append(_run_vm_task(cluster_name, resource))

        return await asyncio.gather(*tasks, return_exceptions=True)  # Gather coroutines

    try:
        journal_messages.append("\n## Virtual Machine Discovery")

        # Process each cluster
        for cluster in cluster_resources:
            cluster_name = list(cluster.keys())[0]
            resources = cluster[cluster_name]
            vm_count = len([r for r in resources if r.get("type") in ("qemu", "lxc")])

            journal_messages += [
                f"\n### Processing Cluster: {cluster_name}",
                f"- Found {vm_count} virtual machines",
            ]

            total_vms += vm_count

        journal_messages += [
            "\n## Virtual Machine Processing",
            f"- Total VMs to process: {total_vms}",
        ]

        # Return the created virtual machines.
        result_list = await asyncio.gather(
            *[_create_cluster_vms(cluster) for cluster in cluster_resources],
            return_exceptions=True,
        )

        logger.info(f"VM Creation Result list: {result_list}")
        for cluster_result in result_list:
            if isinstance(cluster_result, Exception):
                continue
            for result in cluster_result:
                if isinstance(result, Exception):
                    print(
                        "python_exception: ",
                        getattr(result, "python_exception", str(result)),
                    )
                    print("str(result): ", str(result))
                    print("")

        # Flatten the nested results and process them
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
                        journal_messages.append(
                            f"- ✅ Successfully created VM: {vm_result.get('name')} (ID: {vm_result.get('id')})"
                        )
                        flattened_results.append(vm_result)

        # Send end message to websocket
        if all([use_websocket, websocket]):
            await websocket.send_json({"object": "virtual_machine", "end": True})

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
            f"- **Failed**: {failed_vms}",
        ]

        try:
            if sync_process and hasattr(sync_process, "id"):
                journal_entry = await rest_create_async(
                    nb,
                    "/api/extras/journal-entries/",
                    {
                        "assigned_object_type": "netbox_proxbox.syncprocess",
                        "assigned_object_id": sync_process.id,
                        "kind": "info",
                        "comments": "\n".join(journal_messages),
                    },
                )

                if not journal_entry:
                    print("Warning: Journal entry creation returned None")
            else:
                print("Warning: Cannot create journal entry - sync_process is None or has no id")
        except Exception as journal_error:
            print(f"Warning: Failed to create journal entry: {str(journal_error)}")

    return flattened_results


@router.get("/create/stream", response_model=None)
async def create_virtual_machines_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
):
    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await create_virtual_machines(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    custom_fields=custom_fields,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "started",
                    "message": "Starting virtual machines synchronization.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame

            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "completed",
                    "message": "Virtual machines synchronization finished.",
                    "result": {"count": len(result)},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Virtual machines sync completed.",
                    "result": result,
                },
            )
        except Exception as error:
            yield sse_event(
                "error",
                {
                    "step": "virtual-machines",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual machines sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/",
    response_model=list[dict],
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def get_virtual_machines():
    virtual_machine = VirtualMachine()
    return virtual_machine.all()


@router.get(
    "/{id}",
    response_model=dict,
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def get_virtual_machine(id: int):
    try:
        virtual_machine = VirtualMachine().find(id=id)
        if virtual_machine:
            return virtual_machine
        else:
            return {}
    except Exception as error:
        print(f"Error getting virtual machine: {error}")
        return {}


@router.get(
    "/summary/example",
    response_model=VirtualMachineSummary,
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
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
            Disk(
                id="scsi0",
                storage="local-lvm",
                size=102400,
                used=67584,
                usage=66,
                format="raw",
                path="/dev/pve/vm-102-disk-0",
            ),
            Disk(
                id="scsi1",
                storage="local-lvm",
                size=409600,
                used=215040,
                usage=52,
                format="raw",
                path="/dev/pve/vm-102-disk-1",
            ),
        ],
        networks=[
            Network(
                id="net0",
                model="virtio",
                bridge="vmbr0",
                mac="AA:BB:CC:DD:EE:FF",
                ip="10.0.0.102",
                netmask="255.255.255.0",
                gateway="10.0.0.1",
            ),
            Network(
                id="net1",
                model="virtio",
                bridge="vmbr1",
                mac="AA:BB:CC:DD:EE:00",
                ip="192.168.1.102",
                netmask="255.255.255.0",
                gateway="192.168.1.1",
            ),
        ],
        snapshots=[
            Snapshot(
                id="snap1",
                name="pre-update",
                created="2023-05-10 14:30:00",
                description="Before system update",
            ),
            Snapshot(
                id="snap2",
                name="db-config-change",
                created="2023-06-15 09:45:00",
                description="After database configuration change",
            ),
            Snapshot(
                id="snap3",
                name="monthly-backup",
                created="2023-07-01 00:00:00",
                description="Monthly automated snapshot",
            ),
        ],
        backups=[
            Backup(
                id="backup1",
                storage="backup-nfs",
                created="2023-07-01 01:00:00",
                size=75840,
                status="successful",
            ),
            Backup(
                id="backup2",
                storage="backup-nfs",
                created="2023-06-01 01:00:00",
                size=72560,
                status="successful",
            ),
            Backup(
                id="backup3",
                storage="backup-nfs",
                created="2023-05-01 01:00:00",
                size=70240,
                status="successful",
            ),
        ],
    )

    return vm_summary


@router.get(
    "/{id}/summary",
)
async def get_virtual_machine_summary(id: int):
    pass


@router.get("/interfaces/create")
async def create_virtual_machines_interfaces():
    # TODO
    pass


@router.get("/interfaces/ip-address/create")
async def create_virtual_machines_interfaces_ip_address():
    # TODO
    pass


@router.get("/virtual-disks/create")
@sync_process("vm-disks")
async def create_virtual_disks(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_css: bool = False,
    use_websocket: bool = False,
    sync_process=None,
):
    """
    Syncs virtual disks for existing Virtual Machines in NetBox.

    Queries NetBox for VMs with cf_proxmox_vm_id set, fetches their disk
    configuration from Proxmox, and creates/updates Virtual Disk objects.
    """
    result = await sync_virtual_disks(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        tag=tag,
        websocket=websocket,
        use_websocket=use_websocket,
        use_css=use_css,
        sync_process=sync_process,
    )
    return result


@router.get("/virtual-disks/create/stream", response_model=None)
async def create_virtual_disks_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
):
    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await sync_virtual_disks(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                    use_css=False,
                    sync_process=None,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "virtual-disks",
                    "status": "started",
                    "message": "Starting virtual disks synchronization.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame

            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "virtual-disks",
                    "status": "completed",
                    "message": "Virtual disks synchronization finished.",
                    "result": {
                        "count": result.get("count", 0),
                        "created": result.get("created", 0),
                        "updated": result.get("updated", 0),
                        "skipped": result.get("skipped", 0),
                    },
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Virtual disks sync completed.",
                    "result": result,
                },
            )
        except Exception as error:
            yield sse_event(
                "error",
                {
                    "step": "virtual-disks",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual disks sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _volids_from_proxmox_storage_backup_items(items: list[dict]) -> set[str]:
    """Collect Proxmox volume IDs for backup content rows (volid / NetBox volume_id)."""
    out: set[str] = set()
    for item in items:
        if item.get("content") != "backup":
            continue
        vid = item.get("volid")
        if isinstance(vid, str) and vid:
            out.add(vid)
    return out


async def create_netbox_backups(backup, netbox_session: NetBoxSessionDep):
    nb = netbox_session
    vmid_log: str | int | None = None
    try:
        if not isinstance(backup, dict):
            return None
        # Get the virtual machine on NetBox by the VM ID.
        vmid = backup.get("vmid", None)
        vmid_log = vmid
        if not vmid:
            return None

        # Get the virtual machine on NetBox by the VM ID using custom field filter
        vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": int(vmid)},
        )
        virtual_machine = vms[0] if vms else None

        if not virtual_machine:
            return None

        # Process verification data
        verification = backup.get("verification", {})
        verification_state = verification.get("state")
        verification_upid = verification.get("upid")

        # Process storage and volume data
        volume_id = backup.get("volid", None)
        storage_name = volume_id.split(":")[0] if volume_id else None

        # Process creation time
        creation_time = None
        ctime = backup.get("ctime", None)
        if ctime:
            creation_time = datetime.fromtimestamp(ctime).isoformat()

        backup_payload = {
            "storage": storage_name,
            "virtual_machine": virtual_machine.get("id"),
            "subtype": backup.get("subtype"),
            "creation_time": creation_time,
            "size": backup.get("size"),
            "verification_state": verification_state,
            "verification_upid": verification_upid,
            "volume_id": volume_id,
            "notes": backup.get("notes"),
            "vmid": vmid,
            "format": backup.get("format"),
        }

        netbox_backup = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/backups/",
            lookup={"volume_id": volume_id},
            payload=backup_payload,
            schema=NetBoxBackupSyncState,
            current_normalizer=lambda record: {
                "storage": record.get("storage"),
                "virtual_machine": record.get("virtual_machine"),
                "subtype": record.get("subtype"),
                "creation_time": record.get("creation_time"),
                "size": record.get("size"),
                "verification_state": record.get("verification_state"),
                "verification_upid": record.get("verification_upid"),
                "volume_id": record.get("volume_id"),
                "notes": record.get("notes"),
                "vmid": record.get("vmid"),
                "format": record.get("format"),
            },
        )

        # Create a journal entry for the backup
        await rest_create_async(
            nb,
            "/api/extras/journal-entries/",
            {
                "assigned_object_type": "netbox_proxbox.vmbackup",
                "assigned_object_id": netbox_backup.id,
                "kind": "info",
                "comments": f"Backup created for VM {vmid} in storage {storage_name}",
            },
        )

        return netbox_backup

    except Exception as error:
        print(f"Error creating NetBox backup for VM {vmid_log}: {error}")
        return None


async def process_backups_batch(backup_tasks: list, batch_size: int = 10) -> tuple[list, int]:
    """
    Process a list of backup tasks in batches to avoid overwhelming the API.

    Returns:
        (successful_reconcile_results, failure_count) where failures are exceptions from gather.
    """
    results: list = []
    failures = 0
    for i in range(0, len(backup_tasks), batch_size):
        batch = backup_tasks[i : i + batch_size]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)
        for r in batch_results:
            if isinstance(r, Exception):
                failures += 1
            elif r is not None:
                results.append(r)
    return results, failures


async def get_node_backups(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    node: str,
    storage: str,
    netbox_session: NetBoxSessionDep,
    vmid: str | None = None,
) -> tuple[list, set[str]]:
    nb = netbox_session
    """
    Get backups for a specific node and storage.

    Returns:
        (async tasks for NetBox reconcile, set of Proxmox volid strings seen on storage)
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
                            content="backup",
                        )

                        volids = _volids_from_proxmox_storage_backup_items(backups)
                        tasks = [
                            create_netbox_backups(backup, nb)
                            for backup in backups
                            if backup.get("content") == "backup"
                        ]
                        return tasks, volids
                    except Exception as error:
                        print(f"Error getting backups for node {node}: {error}")
                        continue
    return [], set()


@router.get("/backups/create")
async def create_virtual_machine_backups(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    node: Annotated[
        str,
        Query(
            title="Node",
            description="The name of the node to retrieve the storage content for.",
        ),
    ],
    storage: Annotated[
        str,
        Query(
            title="Storage",
            description="The name of the storage to retrieve the content for.",
        ),
    ],
    vmid: Annotated[
        str | None,
        Query(title="VM ID", description="The ID of the VM to retrieve the content for."),
    ] = None,
):
    backup_tasks, _volids = await get_node_backups(
        pxs, cluster_status, node, storage, netbox_session=netbox_session, vmid=vmid
    )
    if not backup_tasks:
        raise ProxboxException(message="Node or Storage not found.")

    reconciled, _failures = await process_backups_batch(backup_tasks)
    return reconciled


async def _create_all_virtual_machine_backups(
    netbox_session,
    pxs,
    cluster_status,
    tag,
    delete_nonexistent_backup=False,
    websocket=None,
    use_websocket=False,
):
    """Internal function that handles backup sync with optional websocket support."""
    nb = netbox_session
    start_time = datetime.now()
    sync_process = None
    results = []
    journal_messages = []  # Store all journal messages
    failure_count = 0
    deleted_count = 0  # Track number of deleted backups
    backup_sync_ok = False

    try:
        # Create sync process
        sync_process = rest_create(
            nb,
            "/api/plugins/proxbox/sync-processes/",
            {
                "name": f"sync-virtual-machines-backups-{start_time}",
                "sync_type": "vm-backups",
                "status": "not-started",
                "started_at": str(start_time),
                "completed_at": None,
                "runtime": None,
                "tags": [tag.id],
            },
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

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "backups",
                    "status": "started",
                    "message": "Starting backup synchronization.",
                }
            )

        journal_messages.append("\n## Backup Discovery")
        all_backup_tasks = []
        proxmox_backups = set()  # Store all Proxmox backup identifiers

        # Process each Proxmox cluster
        for proxmox, cluster in zip(pxs, cluster_status):
            # Get all storage names that have 'backup' in the content
            storage_list = [
                {
                    "storage": storage_dict.get("storage"),
                    "nodes": storage_dict.get("nodes", "all"),
                }
                for storage_dict in proxmox.session.storage.get()
                if "backup" in storage_dict.get("content")
            ]

            journal_messages.append(f"\n### Processing Cluster: {cluster.name}")
            journal_messages.append(f"- Found {len(storage_list)} backup storages")

            # Process each cluster node
            if cluster and cluster.node_list:
                for cluster_node in cluster.node_list:
                    # Process each storage
                    for storage in storage_list:
                        if storage.get("nodes") == "all" or cluster_node.name in storage.get(
                            "nodes", []
                        ):
                            try:
                                node_backup_tasks, node_volids = await get_node_backups(
                                    pxs=pxs,
                                    cluster_status=cluster_status,
                                    node=cluster_node.name,
                                    storage=storage.get("storage"),
                                    netbox_session=nb,
                                )
                                all_backup_tasks.extend(node_backup_tasks)
                                proxmox_backups.update(node_volids)

                                journal_messages.append(
                                    f"- Node `{cluster_node.name}` in storage `{storage.get('storage')}`: Found {len(node_backup_tasks)} backups"
                                )

                            except Exception as error:
                                error_msg = f"Error processing backups for node {cluster_node.name} and storage {storage.get('storage')}: {str(error)}"
                                journal_messages.append(f"  - ❌ {error_msg}")
                                continue

        if not all_backup_tasks:
            error_msg = "No backups found to process"
            journal_messages.append(f"\n### ⚠️ Warning\n{error_msg}")
            if use_websocket and websocket:
                await websocket.send_json(
                    {
                        "step": "backups",
                        "status": "warning",
                        "message": error_msg,
                    }
                )
            raise ProxboxException(message=error_msg)

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "backups",
                    "status": "discovered",
                    "message": f"Found {len(all_backup_tasks)} backups to process.",
                    "count": len(all_backup_tasks),
                }
            )

        journal_messages.append("\n## Backup Processing")
        journal_messages.append(f"- Total backups to process: {len(all_backup_tasks)}")

        # Process all backups in batches
        results, failure_count = await process_backups_batch(all_backup_tasks)

        journal_messages.append(
            f"- Reconciled {len(results)} backup record(s) in NetBox (create/update via API)"
        )
        if failure_count:
            journal_messages.append(f"- Tasks that raised errors: {failure_count}")

        # Handle deletion of nonexistent backups if requested
        if delete_nonexistent_backup:
            journal_messages.append("\n## Deleting Nonexistent Backups")
            try:
                # Get all backups from NetBox
                netbox_backups = rest_list(nb, "/api/plugins/proxbox/backups/")
                skipped_no_volid = 0

                for backup in netbox_backups:
                    vid = backup.volume_id
                    if not vid:
                        skipped_no_volid += 1
                        continue
                    if vid not in proxmox_backups:
                        try:
                            # Delete the backup
                            backup.delete()
                            deleted_count += 1
                            journal_messages.append(
                                f"- Deleted backup for VM ID {backup.vmid} in storage {backup.storage} (volume: {backup.volume_id})"
                            )
                        except Exception as error:
                            journal_messages.append(
                                f"- ❌ Failed to delete backup for VM ID {backup.vmid}: {str(error)}"
                            )

                if skipped_no_volid:
                    journal_messages.append(
                        f"- Skipped {skipped_no_volid} NetBox backup(s) with empty volume_id "
                        "(cannot match Proxmox)"
                    )

                if deleted_count > 0:
                    journal_messages.append(f"\nTotal backups deleted: {deleted_count}")
                else:
                    journal_messages.append("\nNo backups needed to be deleted")

            except Exception as error:
                error_msg = f"Error during backup deletion: {str(error)}"
                journal_messages.append(f"\n### ❌ Error\n{error_msg}")
                # Don't raise the exception as this is not critical for the sync process

        backup_sync_ok = True

    except Exception as error:
        error_msg = f"Error during backup sync: {str(error)}"
        journal_messages.append(f"\n### ❌ Error\n{error_msg}")
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "backups",
                    "status": "failed",
                    "message": error_msg,
                    "error": str(error),
                }
            )
        raise ProxboxException(message=error_msg)

    finally:
        # Always update sync process status
        if sync_process:
            end_time = datetime.now()
            sync_process.completed_at = str(end_time)
            sync_process.runtime = float((end_time - start_time).total_seconds())
            if backup_sync_ok:
                sync_process.status = "completed"
            elif sync_process.status == "syncing":
                sync_process.status = "failed"
            sync_process.save()

            # Add final summary
            journal_messages.append("\n## Process Summary")
            journal_messages.append(f"- **Status**: {sync_process.status}")
            journal_messages.append(f"- **Runtime**: {sync_process.runtime} seconds")
            journal_messages.append(f"- **End Time**: {end_time}")
            journal_messages.append(
                f"- **Backup tasks OK**: {len(results)} reconciled, {failure_count} task error(s)"
            )
            if delete_nonexistent_backup:
                journal_messages.append(f"- **Backups Deleted**: {deleted_count}")

            journal_entry = await rest_create_async(
                nb,
                "/api/extras/journal-entries/",
                {
                    "assigned_object_type": "netbox_proxbox.syncprocess",
                    "assigned_object_id": sync_process.id,
                    "kind": "info",
                    "comments": "\n".join(journal_messages),
                },
            )

            if not journal_entry:
                print("Warning: Journal entry creation returned None")

            if use_websocket and websocket and backup_sync_ok:
                await websocket.send_json(
                    {
                        "step": "backups",
                        "status": "completed",
                        "message": (
                            f"Backup sync completed. {len(results)} reconciled, "
                            f"{failure_count} task error(s), {deleted_count} deleted."
                        ),
                        "result": {
                            "reconciled": len(results),
                            "failed_tasks": failure_count,
                            "deleted": deleted_count,
                        },
                    }
                )

    print("Syncing Backups Finished.")
    return results


@router.get("/backups/all/create")
async def create_all_virtual_machine_backups(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    delete_nonexistent_backup: Annotated[
        bool,
        Query(
            title="Delete Nonexistent Backup",
            description="If true, deletes backups that exist in NetBox but not in Proxmox.",
        ),
    ] = False,
):
    return await _create_all_virtual_machine_backups(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        tag=tag,
        delete_nonexistent_backup=delete_nonexistent_backup,
    )


@router.get("/backups/all/create/stream", response_model=None)
async def create_all_virtual_machine_backups_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    delete_nonexistent_backup: Annotated[
        bool,
        Query(
            title="Delete Nonexistent Backup",
            description="If true, deletes backups that exist in NetBox but not in Proxmox.",
        ),
    ] = False,
):
    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await _create_all_virtual_machine_backups(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    tag=tag,
                    delete_nonexistent_backup=delete_nonexistent_backup,
                    websocket=bridge,
                    use_websocket=True,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "backups",
                    "status": "started",
                    "message": "Starting backup synchronization.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame
            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "backups",
                    "status": "completed",
                    "message": "Backup synchronization finished.",
                    "result": {"count": len(result) if result else 0},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Backup sync completed.",
                    "result": result,
                },
            )
        except Exception as error:
            yield sse_event(
                "error",
                {
                    "step": "backups",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Backup sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _create_all_virtual_machine_snapshots(
    netbox_session,
    pxs,
    cluster_status,
    tag,
    websocket=None,
    use_websocket=False,
):
    """Internal function that handles snapshot sync with optional websocket support."""
    nb = netbox_session
    created_count = 0

    try:
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "snapshots",
                    "status": "started",
                    "message": "Starting snapshot synchronization.",
                }
            )

        result = await sync_snapshots(
            netbox_session=nb,
            pxs=pxs,
            cluster_status=cluster_status,
            tag=tag,
            websocket=websocket,
            use_websocket=use_websocket,
            use_css=False,
        )

        if result:
            created_count = result.get("created", 0)

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "snapshots",
                    "status": "completed",
                    "message": f"Snapshot synchronization finished. Created/updated: {created_count}",
                    "count": created_count,
                }
            )

        return result

    except Exception as error:
        error_msg = f"Error during snapshot sync: {str(error)}"
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "snapshots",
                    "status": "failed",
                    "message": error_msg,
                }
            )
        raise ProxboxException(message=error_msg)


@router.get("/snapshots/create")
async def create_virtual_machine_snapshots(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    vmid: Annotated[
        int | None,
        Query(title="VM ID", description="The ID of the VM to retrieve snapshots for."),
    ] = None,
    node: Annotated[
        str | None,
        Query(title="Node", description="The name of the node."),
    ] = None,
):
    return await sync_snapshots(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        tag=tag,
        vmid=vmid,
        node=node,
    )


@router.get("/snapshots/all/create")
async def create_all_virtual_machine_snapshots(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
):
    return await _create_all_virtual_machine_snapshots(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        tag=tag,
    )


@router.get("/snapshots/all/create/stream", response_model=None)
async def create_all_virtual_machine_snapshots_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
):
    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await _create_all_virtual_machine_snapshots(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "snapshots",
                    "status": "started",
                    "message": "Starting snapshot synchronization.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame
            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "snapshots",
                    "status": "completed",
                    "message": "Snapshot synchronization finished.",
                    "result": {"created": result.get("created", 0) if result else 0},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Snapshot sync completed.",
                    "result": result,
                },
            )
        except Exception as error:
            yield sse_event(
                "error",
                {
                    "step": "snapshots",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Snapshot sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
