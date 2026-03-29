"""DCIM route handlers for device and interface synchronization."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.netbox_rest import nested_tag_payload, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxInterfaceSyncState, NetBoxIpAddressSyncState

# Proxmox Deps
from proxbox_api.routes.proxmox.nodes import ProxmoxNodeInterfacesDep
from proxbox_api.services.sync.devices import ProxmoxCreateDevicesDep
from proxbox_api.services.sync.devices import create_proxmox_devices
from proxbox_api.session.netbox import NetBoxAsyncSessionDep
from proxbox_api.session.netbox import NetBoxSessionDep
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep
from proxbox_api.utils.streaming import sse_event

router = APIRouter()


@router.get("/devices")
async def get_devices():
    return {"message": "Devices created"}


@router.get(
    "/devices/create",
    response_model=list[dict],
    response_model_exclude={"websocket"},
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def create_devices(proxmox_create_devices_dep: ProxmoxCreateDevicesDep):
    return proxmox_create_devices_dep


@router.get("/devices/create/stream", response_model=None)
async def create_devices_stream(
    netbox_session: NetBoxSessionDep,
    clusters_status: ClusterStatusDep,
    tag: ProxboxTagDep,
):
    async def event_stream():
        try:
            yield sse_event(
                "step",
                {
                    "step": "devices",
                    "status": "started",
                    "message": "Starting devices synchronization.",
                },
            )
            result = await create_proxmox_devices(
                netbox_session=netbox_session,
                clusters_status=clusters_status,
                tag=tag,
                use_websocket=False,
            )
            yield sse_event(
                "step",
                {
                    "step": "devices",
                    "status": "completed",
                    "message": "Devices synchronization finished.",
                    "result": {"count": len(result)},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Devices sync completed.",
                    "result": result,
                },
            )
        except Exception as error:
            yield sse_event(
                "error",
                {
                    "step": "devices",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Devices sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def create_interface_and_ip(
    netbox_session: NetBoxAsyncSessionDep, tag: ProxboxTagDep, node_interface, node
):
    interface_type_mapping = {
        "lo": "loopback",
        "bridge": "bridge",
        "bond": "lag",
        "vlan": "virtual",
    }

    node_cidr = getattr(node_interface, "cidr", None)

    node_data = node if isinstance(node, dict) else {}

    interface = await rest_reconcile_async(
        netbox_session,
        "/api/dcim/interfaces/",
        lookup={
            "device_id": node_data.get("id", 0),
            "name": node_interface.iface,
        },
        payload={
            "device": node_data.get("id", 0),
            "name": str(node_interface.iface),
            "status": "active",
            "type": interface_type_mapping.get(node_interface.type, "other"),
            "tags": nested_tag_payload(tag),
        },
        schema=NetBoxInterfaceSyncState,
        current_normalizer=lambda record: {
            "device": record.get("device"),
            "name": record.get("name"),
            "status": record.get("status"),
            "type": record.get("type"),
            "tags": record.get("tags"),
        },
    )
    interface_id = getattr(interface, "id", None) or interface.get("id", None)

    if node_cidr and interface_id is not None:
        await rest_reconcile_async(
            netbox_session,
            "/api/ipam/ip-addresses/",
            lookup={"address": node_cidr},
            payload={
                "address": node_cidr,
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": int(interface_id),
                "status": "active",
                "tags": nested_tag_payload(tag),
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

    return interface


@router.get(
    "/devices/{node}/interfaces/create",
    response_model=list[dict],
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def create_proxmox_device_interfaces(
    node: str,
    nodes: ProxmoxCreateDevicesDep,
    netbox_session: NetBoxAsyncSessionDep,
    tag: ProxboxTagDep,
    node_interfaces: ProxmoxNodeInterfacesDep,
):
    node = None
    for device in nodes:
        node = device
        break

    interfaces = await asyncio.gather(
        *[
            create_interface_and_ip(netbox_session, tag, node_interface, node)
            for node_interface in node_interfaces
        ]
    )
    return [
        interface.dict() if hasattr(interface, "dict") else interface for interface in interfaces
    ]


ProxmoxCreateDeviceInterfacesDep = Annotated[list[dict], Depends(create_proxmox_device_interfaces)]


@router.get("/devices/interfaces/create")
async def create_all_devices_interfaces(
    # nodes: ProxmoxCreateDevicesDep,
    # node_interfaces: ProxmoxNodeInterfacesDep,
):
    return {
        "message": "Endpoint currently not working. Use /devices/{node}/interfaces/create instead."
    }
