"""DCIM route handlers for device and interface synchronization."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.enum.status_mapping import NetBoxInterfaceType
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
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_stream_generator

router = APIRouter()


@router.get("/devices")
async def get_devices():
    return {"message": "Devices created"}


@router.get(
    "/devices/create",
    response_model=list[dict[str, object]],
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
    fetch_max_concurrency: int | None = Query(
        default=None,
        title="Max Fetch Concurrency",
        description="Accepted for API consistency; device sync does not use fetch concurrency.",
    ),
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
        async for frame in sse_stream_generator(bridge, sync_task, "devices"):
            yield frame

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
            "type": NetBoxInterfaceType.from_proxmox(node_interface.type or ""),
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
    response_model=list[dict[str, object]],
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


ProxboxCreateDeviceInterfacesDep = Annotated[list[dict], Depends(create_proxmox_device_interfaces)]


async def _emit_node_interface_event(websocket, use_websocket: bool, payload: dict) -> None:
    """Send a node interface websocket event when streaming is enabled."""
    if use_websocket and websocket:
        await websocket.send_json(payload)


async def _sync_node_interfaces_for_node(
    netbox_session: NetBoxAsyncSessionDep,
    tag_refs: list[dict[str, object]],
    node_obj,
    websocket=None,
    use_websocket: bool = False,
) -> list[dict]:
    """Sync all interfaces for a single node and emit optional websocket updates."""
    from proxbox_api.services.sync.network import sync_node_interface_and_ip

    results: list[dict] = []
    node_name = node_obj.name

    await _emit_node_interface_event(
        websocket,
        use_websocket,
        {
            "object": "node_interface",
            "data": {
                "completed": False,
                "sync_status": "syncing",
                "rowid": node_name,
                "name": node_name,
            },
        },
    )

    try:
        node_networks = node_obj.network or []
    except Exception:
        node_networks = []

    if not node_networks:
        await _emit_node_interface_event(
            websocket,
            use_websocket,
            {
                "object": "node_interface",
                "data": {
                    "completed": True,
                    "rowid": node_name,
                    "name": node_name,
                    "warning": "No network interfaces found",
                },
            },
        )
        return results

    device_record: dict = {"id": getattr(node_obj, "id", None), "name": node_name}
    for iface_data in node_networks:
        iface_name = str(getattr(iface_data, "iface", "") or "")
        if not iface_name:
            continue

        try:
            result = await sync_node_interface_and_ip(
                nb=netbox_session,
                device=device_record,
                interface_name=iface_name,
                interface_config={
                    "type": getattr(iface_data, "type", "other"),
                    "cidr": getattr(iface_data, "cidr", None),
                    "address": getattr(iface_data, "address", None),
                    "vlan_id": getattr(iface_data, "vlan_id", None),
                    "bridge": getattr(iface_data, "iface", None),
                },
                tag_refs=tag_refs,
            )
            results.append(result)

            await _emit_node_interface_event(
                websocket,
                use_websocket,
                {
                    "object": "interface",
                    "data": {
                        "completed": True,
                        "rowid": iface_name,
                        "name": iface_name,
                        "netbox_id": result.get("id"),
                        "device": node_name,
                        "ip_address": result.get("ip_address"),
                    },
                },
            )
        except Exception as exc:
            error_detail = getattr(exc, "detail", str(exc))
            error_msg = f"{type(exc).__name__}: {error_detail}"
            logger.warning(
                "Failed to sync interface %s on node %s: %s",
                iface_name,
                node_name,
                error_msg,
            )
            await _emit_node_interface_event(
                websocket,
                use_websocket,
                {
                    "object": "interface",
                    "data": {
                        "completed": False,
                        "rowid": iface_name,
                        "name": iface_name,
                        "error": str(exc),
                    },
                },
            )

    await _emit_node_interface_event(
        websocket,
        use_websocket,
        {
            "object": "node_interface",
            "data": {
                "completed": True,
                "rowid": node_name,
                "name": node_name,
                "count": len(results),
            },
        },
    )

    return results


async def create_all_device_interfaces(
    netbox_session: NetBoxAsyncSessionDep,
    tag: ProxboxTagDep,
    clusters_status: ClusterStatusDep,
    websocket=None,
    use_websocket: bool = False,
) -> list[dict]:
    """Sync all Proxmox node interfaces and their IP addresses across all clusters.

    Args:
        netbox_session: NetBox async session.
        tag: Proxbox tag reference.
        clusters_status: All cluster status objects from Proxmox.
        websocket: Optional WebSocketSSEBridge for progress events.
        use_websocket: Whether to emit progress events.

    Returns:
        List of all synced interface records.
    """
    tag_refs = nested_tag_payload(tag)
    all_results: list[dict] = []

    if not clusters_status:
        return all_results

    for cluster_status in clusters_status:
        if not cluster_status or not cluster_status.node_list:
            continue

        for node_obj in cluster_status.node_list:
            all_results.extend(
                await _sync_node_interfaces_for_node(
                    netbox_session,
                    tag_refs,
                    node_obj,
                    websocket=websocket,
                    use_websocket=use_websocket,
                )
            )

    if use_websocket and websocket:
        await websocket.send_json({"object": "node_interface", "end": True})

    return all_results


@router.get("/devices/interfaces/create")
async def create_all_devices_interfaces(
    netbox_session: NetBoxSessionDep,
    clusters_status: ClusterStatusDep,
    tag: ProxboxTagDep,
):
    """Sync network interfaces for all Proxmox nodes (dcim.Device interfaces).

    Iterates through all cluster nodes and syncs their network interfaces
    and IP addresses to NetBox dcim.Interface and ipam.IPAddress records.
    """
    results = await create_all_device_interfaces(
        netbox_session=netbox_session,
        tag=tag,
        clusters_status=clusters_status,
    )
    return results


@router.get("/devices/interfaces/create/stream", response_model=None)
async def create_all_devices_interfaces_stream(
    netbox_session: NetBoxSessionDep,
    clusters_status: ClusterStatusDep,
    tag: ProxboxTagDep,
):
    """Streaming endpoint for syncing all Proxmox node interfaces.

    Emits SSE step events per node and per interface as they are synced.
    """

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await create_all_device_interfaces(
                    netbox_session=netbox_session,
                    tag=tag,
                    clusters_status=clusters_status,
                    websocket=bridge,
                    use_websocket=True,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        async for frame in sse_stream_generator(bridge, sync_task, "node-interfaces"):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
