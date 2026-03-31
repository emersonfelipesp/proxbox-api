"""Virtual machine creation sync and SSE stream endpoints."""

# FastAPI Imports
import asyncio

from fastapi import APIRouter
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
from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxDeviceRoleSyncState,
    NetBoxIpAddressSyncState,
    NetBoxVirtualDiskSyncState,
    NetBoxVirtualMachineCreateBody,
    NetBoxVirtualMachineInterfaceSyncState,
    ProxmoxVmConfigInput,
)
from proxbox_api.routes.extras import CreateCustomFieldsDep  # Create Custom Fields
from proxbox_api.routes.proxmox import (
    get_vm_config,  # Get VM Config
)  # Get Proxmox Node Storage Content
from proxbox_api.routes.proxmox.cluster import (
    ClusterResourcesDep,
    ClusterStatusDep,
)  # Cluster Status and Resources
from proxbox_api.routes.virtualization.virtual_machines.helpers import resolve_vm_sync_concurrency
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
from proxbox_api.services.sync.virtual_machines import (
    build_netbox_virtual_machine_payload,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep  # Sessions
from proxbox_api.utils import return_status_html
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

router = APIRouter()


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
):
    """
    Creates a new virtual machine in Netbox.
    """

    nb = netbox_session

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

            logger.debug("VM deps cluster=%s device=%s role=%s", cluster, device, role)

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

        logger.debug("Reconciled virtual_machine=%s", virtual_machine)

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

    max_concurrency = resolve_vm_sync_concurrency()
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
        # Process each cluster
        for cluster in cluster_resources:
            cluster_name = list(cluster.keys())[0]
            resources = cluster[cluster_name]
            vm_count = len([r for r in resources if r.get("type") in ("qemu", "lxc")])

            total_vms += vm_count

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
                    logger.warning(
                        "VM sub-task failed: %s",
                        getattr(result, "python_exception", str(result)),
                    )

        # Flatten the nested results and process them
        for cluster_results in result_list:
            if isinstance(cluster_results, Exception):
                failed_vms += 1
            else:
                # cluster_results is a list of VM creation results
                for vm_result in cluster_results:
                    if isinstance(vm_result, Exception):
                        failed_vms += 1
                    else:
                        successful_vms += 1
                        flattened_results.append(vm_result)

        # Send end message to websocket
        if all([use_websocket, websocket]):
            await websocket.send_json({"object": "virtual_machine", "end": True})

        # Clear cache after creating virtual machines
        global_cache.clear_cache()

        logger.info(
            "VM sync summary: total=%s ok=%s failed=%s",
            total_vms,
            successful_vms,
            failed_vms,
        )

    except Exception as error:
        error_msg = f"Error during VM sync: {str(error)}"
        raise ProxboxException(message=error_msg)

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
                    "result": {"count": len(result)},
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
