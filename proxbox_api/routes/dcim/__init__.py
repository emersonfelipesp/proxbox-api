"""DCIM route handlers for device and interface synchronization."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import (
    ProxboxTagDep,
    ResolvedSyncBehaviorFlagsDep,
    ResolvedSyncOverwriteFlagsDep,
)
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
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.devices import ProxmoxCreateDevicesDep, create_proxmox_devices
from proxbox_api.session.netbox import NetBoxAsyncSessionDep, NetBoxSessionDep
from proxbox_api.session.proxmox import ProxmoxSessionsDep
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
    overwrite_flags: ResolvedSyncOverwriteFlagsDep = SyncOverwriteFlags(),
):
    """Stream device synchronization progress as SSE events.

    All `overwrite_device_*` controls are exposed via the `overwrite_flags` query
    group (FastAPI flattens it into individual query parameters). Unlike the VM
    sync routes, there are no top-level flat `overwrite_device_*` query params on
    this route; consumers should set the desired flag through the group, e.g.
    `?overwrite_device_role=false`.
    """

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
                    overwrite_device_role=overwrite_flags.overwrite_device_role,
                    overwrite_device_type=overwrite_flags.overwrite_device_type,
                    overwrite_device_tags=overwrite_flags.overwrite_device_tags,
                    overwrite_flags=overwrite_flags,
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


async def _sync_node_network_topology(
    netbox_session,
    tag_refs: list[dict[str, object]],
    node_obj,
    proxmox_session,
    websocket=None,
    use_websocket: bool = False,
) -> list[dict]:
    """Reconcile a node's full ``/nodes/{node}/network`` topology in one pass.

    Feeds ``sync_node_network`` the **raw** ``/nodes/{node}/network`` payload
    (hyphenated keys such as ``vlan-id``/``vlan-raw-device`` plus
    ``bridge_ports``/``bond_slaves``/``options``/``active``/``cidr6``) obtained
    via a direct proxmox-sdk call, not the normalized ``node_obj.network`` SDK
    model whose fields the topology reconcile does not surface.
    """
    from proxbox_api.netbox_rest import rest_first_async
    from proxbox_api.proxmox_async import resolve_async
    from proxbox_api.services.sync.network import sync_node_network

    node_name = node_obj.name

    # sync_node_network writes dcim.Interface rows keyed by the NetBox device id,
    # so resolve the device by node name (node_obj.id is the Proxmox id, not the
    # NetBox device PK).
    device = await rest_first_async(
        netbox_session,
        "/api/dcim/devices/",
        query={"name": node_name, "limit": 1},
    )
    device_id = device.get("id") if isinstance(device, dict) else getattr(device, "id", None)
    if device_id is None:
        logger.warning(
            "Skipping node network topology sync for %s: NetBox device not found",
            node_name,
        )
        return []

    try:
        raw_network = await resolve_async(
            proxmox_session.session(f"/nodes/{node_name}/network").get()
        )
    except Exception as exc:
        logger.warning("Failed to fetch raw network for node %s: %s", node_name, exc)
        return []

    try:
        results = await sync_node_network(
            netbox_session,
            {"id": device_id, "name": node_name},
            list(raw_network or []),
            tag_refs,
        )
    except Exception as exc:
        logger.warning("Failed to sync node network topology for %s: %s", node_name, exc)
        return []

    for result in results:
        await _emit_node_interface_event(
            websocket,
            use_websocket,
            {
                "object": "interface",
                "data": {
                    "completed": True,
                    "rowid": result.get("name"),
                    "name": result.get("name"),
                    "netbox_id": result.get("id"),
                    "device": node_name,
                    "ip_address": (result.get("ip_addresses") or [None])[0],
                },
            },
        )

    return results


async def _sync_node_interfaces_for_node(
    netbox_session: NetBoxAsyncSessionDep,
    tag_refs: list[dict[str, object]],
    node_obj,
    websocket=None,
    use_websocket: bool = False,
    proxmox_session=None,
    sync_full_topology: bool = False,
) -> list[dict]:
    """Sync all interfaces for a single node and emit optional websocket updates.

    When ``sync_full_topology`` is set (the ``sync_node_interfaces`` behavior
    flag) and a ``proxmox_session`` is provided, the node's full
    ``/nodes/{node}/network`` topology is reconciled via ``sync_node_network``
    in a single pass. Otherwise the historical per-interface loop over the
    normalized SDK model is used, keeping existing deployments unchanged.
    """
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

    if sync_full_topology and proxmox_session is not None:
        results = await _sync_node_network_topology(
            netbox_session,
            tag_refs,
            node_obj,
            proxmox_session,
            websocket=websocket,
            use_websocket=use_websocket,
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
    pxs=None,
    behavior_flags=None,
) -> list[dict]:
    """Sync all Proxmox node interfaces and their IP addresses across all clusters.

    Args:
        netbox_session: NetBox async session.
        tag: Proxbox tag reference.
        clusters_status: All cluster status objects from Proxmox.
        websocket: Optional WebSocketSSEBridge for progress events.
        use_websocket: Whether to emit progress events.
        pxs: Proxmox sessions, one per cluster in ``clusters_status`` order
            (both derive from the same ``ProxmoxSessionsDep``). Required for the
            ``sync_node_interfaces`` full-topology path, which needs the raw
            ``/nodes/{node}/network`` payload.
        behavior_flags: Resolved ``SyncBehaviorFlags``; ``sync_node_interfaces``
            selects the full-topology reconcile.

    Returns:
        List of all synced interface records.
    """
    tag_refs = nested_tag_payload(tag)
    all_results: list[dict] = []

    if not clusters_status:
        return all_results

    sync_full_topology = bool(getattr(behavior_flags, "sync_node_interfaces", False))
    # cluster_status() builds one ClusterStatusSchema per Proxmox session, in
    # session order, so clusters_status and pxs line up positionally. Any
    # session in a cluster can serve /nodes/{node}/network for that cluster.
    sessions = list(pxs) if pxs is not None else []

    for index, cluster_status in enumerate(clusters_status):
        if not cluster_status or not cluster_status.node_list:
            continue

        proxmox_session = sessions[index] if index < len(sessions) else None

        for node_obj in cluster_status.node_list:
            all_results.extend(
                await _sync_node_interfaces_for_node(
                    netbox_session,
                    tag_refs,
                    node_obj,
                    websocket=websocket,
                    use_websocket=use_websocket,
                    proxmox_session=proxmox_session,
                    sync_full_topology=sync_full_topology,
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
    pxs: ProxmoxSessionsDep,
    behavior_flags: ResolvedSyncBehaviorFlagsDep,
):
    """Sync network interfaces for all Proxmox nodes (dcim.Device interfaces).

    Iterates through all cluster nodes and syncs their network interfaces
    and IP addresses to NetBox dcim.Interface and ipam.IPAddress records.
    With the ``sync_node_interfaces`` behavior flag set, the full
    ``/nodes/{node}/network`` topology is reconciled instead.
    """
    results = await create_all_device_interfaces(
        netbox_session=netbox_session,
        tag=tag,
        clusters_status=clusters_status,
        pxs=pxs,
        behavior_flags=behavior_flags,
    )
    return results


@router.get("/devices/interfaces/create/stream", response_model=None)
async def create_all_devices_interfaces_stream(
    netbox_session: NetBoxSessionDep,
    clusters_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    pxs: ProxmoxSessionsDep,
    behavior_flags: ResolvedSyncBehaviorFlagsDep,
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
                    pxs=pxs,
                    behavior_flags=behavior_flags,
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
