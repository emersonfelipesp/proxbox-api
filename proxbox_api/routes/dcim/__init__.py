"""DCIM route handlers for device and interface synchronization."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import nested_tag_payload, rest_patch_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxInterfaceSyncState,
    NetBoxIpAddressSyncState,
    NetBoxVlanSyncState,
)
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep

# Proxmox Deps
from proxbox_api.routes.proxmox.nodes import ProxmoxNodeInterfacesDep
from proxbox_api.services.sync.devices import ProxmoxCreateDevicesDep, create_proxmox_devices
from proxbox_api.session.netbox import NetBoxAsyncSessionDep, NetBoxSessionDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

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
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await create_proxmox_devices(
                    netbox_session=netbox_session,
                    clusters_status=clusters_status,
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
                    "step": "devices",
                    "status": "started",
                    "message": "Starting devices synchronization.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame
            result = await sync_task
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

    # Resolve VLAN for node interfaces with type=vlan and a vlan_id
    vlan_nb_id: int | None = None
    iface_type = getattr(node_interface, "type", None)
    vlan_id_raw = getattr(node_interface, "vlan_id", None)
    if iface_type == "vlan" and vlan_id_raw is not None:
        try:
            vlan_vid = int(vlan_id_raw)
            vlan_record = await rest_reconcile_async(
                netbox_session,
                "/api/ipam/vlans/",
                lookup={"vid": vlan_vid},
                payload={
                    "vid": vlan_vid,
                    "name": f"VLAN {vlan_vid}",
                    "status": "active",
                    "tags": nested_tag_payload(tag),
                },
                schema=NetBoxVlanSyncState,
                current_normalizer=lambda record: {
                    "vid": record.get("vid"),
                    "name": record.get("name"),
                    "status": record.get("status"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            )
            vlan_nb_id = (
                vlan_record.get("id")
                if isinstance(vlan_record, dict)
                else getattr(vlan_record, "id", None)
            )
        except Exception as vlan_exc:
            logger.warning(
                "Failed to create/sync VLAN vid=%s for node interface %s: %s",
                vlan_id_raw,
                getattr(node_interface, "iface", "?"),
                vlan_exc,
            )

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
            "untagged_vlan": vlan_nb_id,
            "mode": "access" if vlan_nb_id is not None else None,
            "tags": nested_tag_payload(tag),
        },
        schema=NetBoxInterfaceSyncState,
        current_normalizer=lambda record: {
            "device": record.get("device"),
            "name": record.get("name"),
            "status": record.get("status"),
            "type": record.get("type"),
            "untagged_vlan": record.get("untagged_vlan"),
            "mode": record.get("mode"),
            "tags": record.get("tags"),
        },
    )
    interface_id = getattr(interface, "id", None) or interface.get("id", None)

    ip_record = None
    if node_cidr and interface_id is not None:
        ip_record = await rest_reconcile_async(
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

    return interface, ip_record


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

    results = await asyncio.gather(
        *[
            create_interface_and_ip(netbox_session, tag, node_interface, node)
            for node_interface in node_interfaces
        ]
    )

    # Set primary IP on the device when not already set (user choice is preserved)
    node_data = node if isinstance(node, dict) else {}
    if node_data.get("primary_ip4") is None:
        device_id = node_data.get("id")
        first_ip_id = next(
            (
                (ip.get("id") if isinstance(ip, dict) else getattr(ip, "id", None))
                for _, ip in results
                if ip is not None
            ),
            None,
        )
        if device_id is not None and first_ip_id is not None:
            try:
                await rest_patch_async(
                    netbox_session,
                    "/api/dcim/devices/",
                    device_id,
                    {"primary_ip4": first_ip_id},
                )
            except Exception as exc:
                logger.warning("Failed to set primary_ip4 for device id=%s: %s", device_id, exc)
        elif device_id is not None:
            logger.info("No IP found for device id=%s, skipping primary_ip4 assignment.", device_id)

    return [iface.dict() if hasattr(iface, "dict") else iface for iface, _ in results]


ProxmoxCreateDeviceInterfacesDep = Annotated[list[dict], Depends(create_proxmox_device_interfaces)]


@router.get("/devices/interfaces/create")
async def create_all_devices_interfaces(
    # nodes: ProxmoxCreateDevicesDep,
    # node_interfaces: ProxmoxNodeInterfacesDep,
):
    return {
        "message": "Endpoint currently not working. Use /devices/{node}/interfaces/create instead."
    }
