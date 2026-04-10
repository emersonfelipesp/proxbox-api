"""Virtual machine read/query routes."""

import asyncio

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.logger import logger
from proxbox_api.netbox_sdk_helpers import to_dict
from proxbox_api.routes.extras import CreateCustomFieldsDep
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.schemas.virtualization import (  # Schemas
    CPU,
    Backup,
    Disk,
    Memory,
    Network,
    Snapshot,
    VirtualMachineSummary,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.get(
    "/",
    response_model=list[dict[str, object]],
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def get_virtual_machines(netbox_session: NetBoxSessionDep):
    results = []
    async for item in netbox_session.virtualization.virtual_machines.all():
        results.append(to_dict(item))
    return results


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


@router.get("/interfaces/create")
async def create_virtual_machines_interfaces(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "When true and QEMU guest-agent data is available, VM interface names "
            "are created from guest-agent interface names instead of netX/nicX labels."
        ),
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=(
            "When true, IPv6 link-local addresses (fe80::/64) are ignored during "
            "VM interface IP address selection."
        ),
    ),
):
    from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
        create_only_vm_interfaces,
    )

    results = await create_only_vm_interfaces(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        custom_fields=custom_fields,
        tag=tag,
        websocket=None,
        use_websocket=False,
        use_guest_agent_interface_name=use_guest_agent_interface_name,
        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
    )
    return results


@router.get("/interfaces/create/stream", response_model=None)
async def create_virtual_machines_interfaces_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "When true and QEMU guest-agent data is available, VM interface names "
            "are created from guest-agent interface names instead of netX/nicX labels."
        ),
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=(
            "When true, IPv6 link-local addresses (fe80::/64) are ignored during "
            "VM interface IP address selection."
        ),
    ),
):
    from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
        create_only_vm_interfaces,
    )
    from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await create_only_vm_interfaces(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    custom_fields=custom_fields,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                    use_guest_agent_interface_name=use_guest_agent_interface_name,
                    ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "vm-interfaces",
                    "status": "started",
                    "message": "Starting VM interfaces synchronization.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame
            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "vm-interfaces",
                    "status": "completed",
                    "message": "VM interfaces synchronization finished.",
                    "result": {"count": len(result)},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "VM interfaces sync completed.",
                    "result": result,
                },
            )
        except Exception as error:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            yield sse_event(
                "error",
                {
                    "step": "vm-interfaces",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "VM interfaces sync failed.",
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


@router.get("/interfaces/ip-address/create")
async def create_virtual_machines_interfaces_ip_address(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "When true and QEMU guest-agent data is available, interface names "
            "are taken from guest-agent data."
        ),
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=("When true, IPv6 link-local addresses (fe80::/64) are ignored."),
    ),
):
    from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
        create_only_vm_ip_addresses,
    )

    results = await create_only_vm_ip_addresses(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        custom_fields=custom_fields,
        tag=tag,
        websocket=None,
        use_websocket=False,
        use_guest_agent_interface_name=use_guest_agent_interface_name,
        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
    )
    return results


@router.get("/interfaces/ip-address/create/stream", response_model=None)
async def create_virtual_machines_ip_address_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "When true and QEMU guest-agent data is available, interface names "
            "are taken from guest-agent data."
        ),
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=("When true, IPv6 link-local addresses (fe80::/64) are ignored."),
    ),
):
    from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
        create_only_vm_ip_addresses,
    )
    from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await create_only_vm_ip_addresses(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    custom_fields=custom_fields,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                    use_guest_agent_interface_name=use_guest_agent_interface_name,
                    ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "vm-ip-addresses",
                    "status": "started",
                    "message": "Starting VM IP address synchronization.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame
            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "vm-ip-addresses",
                    "status": "completed",
                    "message": "VM IP address synchronization finished.",
                    "result": {"count": len(result)},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "VM IP address sync completed.",
                    "result": result,
                },
            )
        except Exception as error:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            yield sse_event(
                "error",
                {
                    "step": "vm-ip-addresses",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "VM IP address sync failed.",
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
    "/{id}",
    response_model=dict,
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def get_virtual_machine(id: int, netbox_session: NetBoxSessionDep):
    try:
        result = await netbox_session.virtualization.virtual_machines.get(id)
        if result:
            return to_dict(result)
        raise HTTPException(status_code=404, detail="Virtual machine not found")
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("Error getting virtual machine id=%s", id)
        raise HTTPException(
            status_code=502,
            detail="Failed to load virtual machine from NetBox",
        ) from error


@router.get("/{id}/summary")
async def get_virtual_machine_summary(id: int):
    raise HTTPException(
        status_code=501,
        detail="Virtual machine summary by id is not implemented yet.",
    )
