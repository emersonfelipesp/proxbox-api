"""DCIM route handlers for device and interface synchronization."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import (
    ProxboxTagDep,
    ResolvedSyncBehaviorFlagsDep,
    ResolvedSyncOverwriteFlagsDep,
    ensure_netbox_sync_dependencies,
)
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import nested_tag_payload, rest_list_async, rest_patch_async
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep

# Proxmox Deps
from proxbox_api.routes.proxmox.nodes import ProxmoxNodeInterfacesDep
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.device_ensure import _effective_cluster_site_id
from proxbox_api.services.sync.devices import ProxmoxCreateDevicesDep, create_proxmox_devices
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.services.sync.network import (
    load_proxmox_node_network,
    sync_node_interface_and_ip,
)
from proxbox_api.session.netbox import NetBoxAsyncSessionDep, NetBoxSessionDep
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_stream_generator

router = APIRouter()


@router.get("/devices")
async def get_devices():
    return {"message": "Devices created"}


@router.get(
    "/devices/create",
    # The NetBox-side support objects (Proxbox tag, cluster type, manufacturer,
    # device type, device role) must exist before the first device
    # write. Declared as a *route-level* dependency rather than a function
    # parameter on purpose: FastAPI solves route-level dependencies ahead of the
    # path operation's own parameters, so the bootstrap completes before
    # ``ProxmoxCreateDevicesDep`` starts writing. A plain parameter gives no such
    # ordering guarantee.
    dependencies=[Depends(ensure_netbox_sync_dependencies)],
    response_model=list[dict[str, object]],
    response_model_exclude={"websocket"},
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def create_devices(proxmox_create_devices_dep: ProxmoxCreateDevicesDep):
    return proxmox_create_devices_dep


@router.get(
    "/devices/create/stream",
    # ``devices`` is the first entry in the plugin's stage order, so on a fresh
    # install this route is routinely the very first NetBox write of a
    # stage-by-stage sync. Without the bootstrap, NetBox rejects every write with
    # "Custom field 'proxmox_last_updated' does not exist for this object type"
    # and the whole device_roles -> clusters -> device_types -> devices chain
    # fails with misleading downstream errors. See the route-level ordering note
    # on ``/devices/create`` above.
    dependencies=[Depends(ensure_netbox_sync_dependencies)],
    response_model=None,
)
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


def _value_from_record(record: object, key: str, default: object = None) -> object:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _serialize_record(record: object) -> dict[str, object]:
    if isinstance(record, dict):
        return dict(record)
    if hasattr(record, "serialize"):
        serialized = record.serialize()
        return dict(serialized) if isinstance(serialized, dict) else {}
    if hasattr(record, "dict"):
        serialized = record.dict()
        return dict(serialized) if isinstance(serialized, dict) else {}
    return dict(getattr(record, "__dict__", {}) or {})


def _relation_id_or_none(value: object) -> int | None:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_relation_id(record: dict[str, object], field: str) -> int | None:
    return _relation_id_or_none(record.get(f"{field}_id") or record.get(field))


def _node_interface_config(node_interface: object) -> dict[str, object]:
    return {
        "type": _value_from_record(node_interface, "type", "other"),
        "cidr": _value_from_record(node_interface, "cidr"),
        "address": _value_from_record(node_interface, "address"),
        "vlan_id": _value_from_record(node_interface, "vlan_id"),
        "bridge": _value_from_record(node_interface, "iface"),
    }


def _cluster_contains_node(cluster_status: object, node_name: str) -> bool:
    return any(
        str(getattr(node, "name", "") or "").strip() == node_name
        for node in getattr(cluster_status, "node_list", None) or []
    )


def _resolve_cluster_status_for_node(
    clusters_status: list[object] | None,
    node_name: str,
    *,
    cluster_name: str | None = None,
) -> object | None:
    matches = [
        cluster_status
        for cluster_status in clusters_status or []
        if _cluster_contains_node(cluster_status, node_name)
        and (
            cluster_name is None
            or str(getattr(cluster_status, "name", "") or "").strip() == cluster_name
        )
    ]
    if len(matches) == 1:
        return matches[0]
    if cluster_name:
        raise ProxboxException(
            message=(
                f"No Proxmox cluster '{cluster_name}' contains node '{node_name}' "
                "for interface sync"
            ),
            detail="Node interface sync must be scoped to the cluster that owns the node.",
        )
    if len(matches) > 1:
        raise ProxboxException(
            message=f"Ambiguous Proxmox node '{node_name}' for interface sync",
            detail=(
                "Multiple clusters contain a node with this name. Pass cluster_name so "
                "the NetBox device can be resolved by that cluster's site."
            ),
        )
    return None


async def _resolve_netbox_cluster_by_name(
    netbox_session: object,
    cluster_name: str,
) -> dict[str, object] | None:
    records = await rest_list_async(
        netbox_session,
        "/api/virtualization/clusters/",
        query={"name": cluster_name, "limit": 2},
    )
    for record in records:
        data = _serialize_record(record)
        if str(data.get("name") or "").strip() == cluster_name:
            return data
    return None


async def _resolve_cluster_scope_for_node(
    netbox_session: object,
    cluster_status: object | None,
) -> tuple[int | None, int | None]:
    if cluster_status is None:
        return None, None

    cluster_name = str(getattr(cluster_status, "name", "") or "").strip()
    fallback_site_id = _relation_id_or_none(getattr(cluster_status, "site_id", None))
    if not cluster_name:
        return fallback_site_id, None

    cluster_record = await _resolve_netbox_cluster_by_name(netbox_session, cluster_name)
    if cluster_record is None:
        if fallback_site_id is not None:
            return fallback_site_id, None
        raise ProxboxException(
            message=f"No NetBox cluster found for Proxmox cluster '{cluster_name}'",
            detail=(
                "Node interface sync requires the NetBox virtualization.Cluster row "
                "created by device sync so same-name devices can be scoped safely."
            ),
        )

    return (
        _effective_cluster_site_id(cluster_record, fallback_site_id=fallback_site_id),
        _relation_id_or_none(cluster_record.get("id")),
    )


async def _resolve_netbox_device_by_name(
    netbox_session: object,
    node_name: str,
    *,
    candidates: list[object] | None = None,
    clusters_status: list[object] | None = None,
    cluster_name: str | None = None,
) -> dict[str, object]:
    cluster_status = _resolve_cluster_status_for_node(
        clusters_status,
        node_name,
        cluster_name=cluster_name,
    )
    site_id, cluster_id = await _resolve_cluster_scope_for_node(netbox_session, cluster_status)
    if site_id is None and cluster_id is None:
        raise ProxboxException(
            message=f"No cluster/site scope found for Proxmox node '{node_name}'",
            detail=(
                "Refusing name-only device lookup for node interface sync. Run device "
                "sync first and pass cluster_name when node names are reused."
            ),
        )

    for candidate in candidates or []:
        candidate_data = _serialize_record(candidate)
        if str(candidate_data.get("name") or "").strip() == node_name:
            candidate_site_id = _record_relation_id(candidate_data, "site")
            candidate_cluster_id = _record_relation_id(candidate_data, "cluster")
            if site_id is not None and candidate_site_id == site_id:
                return candidate_data
            if site_id is None and cluster_id is not None and candidate_cluster_id == cluster_id:
                return candidate_data

    query: dict[str, object] = {"name": node_name, "limit": 2}
    if site_id is not None:
        query["site_id"] = site_id
    elif cluster_id is not None:
        query["cluster_id"] = cluster_id
    records = await rest_list_async(
        netbox_session,
        "/api/dcim/devices/",
        query=query,
    )
    for record in records:
        data = _serialize_record(record)
        data_site_id = _record_relation_id(data, "site")
        data_cluster_id = _record_relation_id(data, "cluster")
        if (
            str(data.get("name") or "").strip() == node_name
            and (site_id is None or data_site_id == site_id)
            and (site_id is not None or cluster_id is None or data_cluster_id == cluster_id)
        ):
            return data

    raise ProxboxException(
        message=f"No NetBox device found for Proxmox node '{node_name}'",
        detail=(
            "Node interface sync requires the NetBox dcim.Device row created by device "
            "sync in the target cluster/site scope."
        ),
    )


async def create_interface_and_ip(
    netbox_session: NetBoxAsyncSessionDep, tag: ProxboxTagDep, node_interface, node
):
    node_data = _serialize_record(node)
    iface_name = str(_value_from_record(node_interface, "iface", "") or "").strip()
    if not iface_name:
        raise ProxboxException(
            message="Cannot sync unnamed Proxmox node interface",
            detail="Proxmox node network payload did not include iface.",
        )

    return await sync_node_interface_and_ip(
        nb=netbox_session,
        device=node_data,
        interface_name=iface_name,
        interface_config=_node_interface_config(node_interface),
        tag_refs=nested_tag_payload(tag),
    )


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
    clusters_status: ClusterStatusDep,
    cluster_name: Annotated[
        str | None,
        Query(
            title="Cluster Name",
            description="Optional cluster name to disambiguate same-name Proxmox nodes.",
        ),
    ] = None,
):
    node_name = node
    node_data = await _resolve_netbox_device_by_name(
        netbox_session,
        node_name,
        candidates=nodes,
        clusters_status=clusters_status,
        cluster_name=cluster_name,
    )

    results = await asyncio.gather(
        *[
            create_interface_and_ip(netbox_session, tag, node_interface, node_data)
            for node_interface in node_interfaces
        ]
    )

    # Set primary IP on the device when not already set (user choice is preserved)
    if node_data.get("primary_ip4") is None:
        device_id = node_data.get("id")
        first_ip_id = next(
            (
                int(result["ip_id"])
                for result in results
                if isinstance(result, dict) and result.get("ip_id") is not None
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

    return [dict(result) for result in results]


ProxboxCreateDeviceInterfacesDep = Annotated[list[dict], Depends(create_proxmox_device_interfaces)]


async def _emit_node_interface_event(websocket, use_websocket: bool, payload: dict) -> None:
    """Send a node interface websocket event when streaming is enabled."""
    if use_websocket and websocket:
        await websocket.send_json(payload)


async def _emit_node_network_error(
    websocket, use_websocket: bool, node_name: str, exc: Exception
) -> None:
    """Emit a node-level failure event mirroring the legacy per-interface error shape."""
    await _emit_node_interface_event(
        websocket,
        use_websocket,
        {
            "object": "node_interface",
            "data": {
                "completed": False,
                "rowid": node_name,
                "name": node_name,
                "error": str(exc),
            },
        },
    )


async def _sync_node_network_topology(
    netbox_session,
    tag_refs: list[dict[str, object]],
    *,
    node_name: str,
    device_record: dict[str, object],
    proxmox_session,
    websocket=None,
    use_websocket: bool = False,
) -> list[dict]:
    """Reconcile a node's full ``/nodes/{node}/network`` topology in one pass.

    Feeds ``sync_node_network`` the **raw** ``/nodes/{node}/network`` payload
    (hyphenated keys such as ``vlan-id``/``vlan-raw-device`` plus
    ``bridge_ports``/``bond_slaves``/``options``/``active``/``cidr6``) obtained
    via a direct proxmox-sdk call, not the normalized ``load_proxmox_node_network``
    payload whose fields the topology reconcile does not surface.
    """
    from proxbox_api.proxmox_async import resolve_async
    from proxbox_api.services.sync.network import sync_node_network

    # sync_node_network writes dcim.Interface rows keyed by the NetBox device id.
    # The caller already resolved the NetBox device for this node.
    device_id = (
        device_record.get("id")
        if isinstance(device_record, dict)
        else getattr(device_record, "id", None)
    )
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
    except ProxboxException:
        raise
    except Exception as exc:
        # A failed fetch means the node topology could not be reconciled at all.
        # Surface it (stream event + raised error) instead of reporting a silent
        # zero-interface success, matching the legacy per-interface path.
        await _emit_node_network_error(websocket, use_websocket, node_name, exc)
        raise ProxboxException(
            message=f"Failed to fetch node network for '{node_name}'",
            detail=f"{type(exc).__name__}: {getattr(exc, 'detail', str(exc))}",
            python_exception=str(exc),
        ) from exc

    try:
        results = await sync_node_network(
            netbox_session,
            {"id": device_id, "name": node_name},
            list(raw_network or []),
            tag_refs,
        )
    except ProxboxException:
        raise
    except Exception as exc:
        await _emit_node_network_error(websocket, use_websocket, node_name, exc)
        raise ProxboxException(
            message=f"Failed to reconcile node network topology for '{node_name}'",
            detail=f"{type(exc).__name__}: {getattr(exc, 'detail', str(exc))}",
            python_exception=str(exc),
        ) from exc

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
    *,
    node_name: str,
    device_record: dict[str, object],
    node_networks: list[object],
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
    caller-provided normalized ``node_networks`` payload is used, keeping
    existing deployments unchanged.
    """
    results: list[dict] = []

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
            node_name=node_name,
            device_record=device_record,
            proxmox_session=proxmox_session,
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

    for iface_data in node_networks:
        iface_name = str(_value_from_record(iface_data, "iface", "") or "").strip()
        if not iface_name:
            continue

        try:
            result = await sync_node_interface_and_ip(
                nb=netbox_session,
                device=device_record,
                interface_name=iface_name,
                interface_config=_node_interface_config(iface_data),
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

    if not results:
        raise ProxboxException(
            message=f"Node interface sync created zero interfaces for node '{node_name}'",
            detail="Proxmox returned node network data, but no dcim.Interface rows were reconciled.",
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


def _resolve_cluster_proxmox_session(pxs: list[object] | None, cluster_name: str) -> object:
    px_list = list(pxs or [])
    if not px_list:
        raise ProxboxException(
            message="No Proxmox sessions available for node interface sync",
            detail="Batch node-interface sync must fetch live node network payloads.",
        )

    px = resolve_proxmox_session(px_list, cluster_name)
    if px is not None:
        return px
    if len(px_list) == 1:
        return px_list[0]

    raise ProxboxException(
        message=f"No Proxmox session found for cluster: {cluster_name}",
        detail="Unable to resolve node interface sync to a Proxmox session.",
    )


async def create_all_device_interfaces(
    netbox_session: NetBoxAsyncSessionDep,
    tag: ProxboxTagDep,
    clusters_status: ClusterStatusDep,
    pxs: list[object] | None = None,
    websocket=None,
    use_websocket: bool = False,
    behavior_flags=None,
) -> list[dict]:
    """Sync all Proxmox node interfaces and their IP addresses across all clusters.

    Args:
        netbox_session: NetBox async session.
        tag: Proxbox tag reference.
        clusters_status: All cluster status objects from Proxmox.
        pxs: Proxmox sessions used to fetch per-node network payloads. Required
            for the ``sync_node_interfaces`` full-topology path, which needs the
            raw ``/nodes/{node}/network`` payload.
        websocket: Optional WebSocketSSEBridge for progress events.
        use_websocket: Whether to emit progress events.
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

    for cluster_status in clusters_status:
        if not cluster_status or not cluster_status.node_list:
            continue

        cluster_name = str(getattr(cluster_status, "name", "") or "").strip()
        proxmox_session = _resolve_cluster_proxmox_session(pxs, cluster_name)
        for node_obj in cluster_status.node_list:
            node_name = str(getattr(node_obj, "name", "") or "").strip()
            if not node_name:
                continue
            device_record = await _resolve_netbox_device_by_name(
                netbox_session,
                node_name,
                clusters_status=[cluster_status],
                cluster_name=cluster_name,
            )
            # The full-topology path re-fetches the raw payload itself, so the
            # normalized loader would be a wasted round-trip when the flag is on.
            node_networks = (
                []
                if sync_full_topology
                else await load_proxmox_node_network(proxmox_session, node_name)
            )
            all_results.extend(
                await _sync_node_interfaces_for_node(
                    netbox_session,
                    tag_refs,
                    node_name=node_name,
                    device_record=device_record,
                    node_networks=node_networks,
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
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
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
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
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
                    pxs=pxs,
                    websocket=bridge,
                    use_websocket=True,
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
