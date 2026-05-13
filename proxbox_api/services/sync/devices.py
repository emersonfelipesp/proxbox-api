"""Device synchronization service from Proxmox nodes to NetBox."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from proxbox_api.cache import global_cache
from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import nested_tag_payload
from proxbox_api.schemas.stream_messages import ErrorCategory, ItemOperation
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.device_ensure import (
    _ensure_cluster,
    _ensure_cluster_type,
    _ensure_device,
    _ensure_device_role,
    _ensure_device_type,
    _ensure_manufacturer,
    _ensure_site,
    _resolve_tenant,
    ensure_proxmox_devices_bulk,
    placement_from_source,
)
from proxbox_api.utils import return_status_html
from proxbox_api.utils.streaming import WebSocketSSEBridge
from proxbox_api.utils.structured_logging import SyncPhaseLogger

__all__ = [
    "_ensure_cluster",
    "_ensure_cluster_type",
    "_ensure_device",
    "_ensure_device_role",
    "_ensure_device_type",
    "_ensure_manufacturer",
    "_ensure_site",
    "_resolve_tenant",
    "placement_from_source",
    "create_proxmox_devices",
    "ProxmoxCreateDevicesDep",
]


async def create_proxmox_devices(  # noqa: C901
    netbox_session: object,
    clusters_status: list[object] | None,
    tag: ProxboxTagDep,
    websocket: Annotated[WebSocketSSEBridge | None, Depends(lambda: None)] = None,
    node: str | None = None,
    use_websocket: bool = False,
    use_css: bool = False,
    overwrite_device_role: bool = True,
    overwrite_device_type: bool = True,
    overwrite_device_tags: bool = True,
    overwrite_flags: SyncOverwriteFlags | None = None,
) -> list[dict[str, object]]:
    """Create and synchronize devices from Proxmox nodes to NetBox."""
    tag_refs = nested_tag_payload(tag)
    nb = netbox_session
    phase_logger = SyncPhaseLogger("device_sync", cluster_mode="proxmox")
    device_list: list[dict[str, object]] = []

    if not clusters_status:
        phase_logger.log_phase("validation", "No cluster status data provided", level="warning")
        return device_list

    bridge: WebSocketSSEBridge | None = (
        websocket if use_websocket and isinstance(websocket, WebSocketSSEBridge) else None
    )

    all_devices: list[dict[str, object]] = []
    device_cluster_map: dict[str, str] = {}
    for cluster_status in clusters_status:
        cluster_name = str(getattr(cluster_status, "name", "") or "").strip()
        cluster_mode = str(getattr(cluster_status, "mode", "") or "").strip().capitalize()
        for node_obj in getattr(cluster_status, "node_list", None) or []:
            device_name = str(getattr(node_obj, "name", "") or "").strip()
            if not device_name:
                continue
            all_devices.append(
                {
                    "name": device_name,
                    "type": "node",
                    "cluster": cluster_name,
                    "cluster_mode": cluster_mode or "Proxmox",
                }
            )
            device_cluster_map[device_name] = cluster_name

    if bridge:
        await bridge.emit_discovery(
            phase="devices",
            items=all_devices,
            message=f"Discovered {len(all_devices)} device(s) to synchronize",
            metadata={"total_devices": len(all_devices)},
        )

    try:
        phase_logger.log_phase("bulk_prerequisites", "Reconciling device dependency phases")
        devices_by_name = await ensure_proxmox_devices_bulk(
            nb,
            clusters_status=clusters_status,
            tag_refs=tag_refs,
            overwrite_device_role=overwrite_device_role,
            overwrite_device_type=overwrite_device_type,
            overwrite_device_tags=overwrite_device_tags,
            overwrite_flags=overwrite_flags,
        )
    except Exception as error:
        error_msg = f"Error during device sync dependency phases: {error}"
        logger.error(error_msg)
        if bridge:
            await bridge.emit_error_detail(
                message=error_msg,
                category=ErrorCategory.INTERNAL,
                phase="devices",
                detail=str(error),
            )
        if isinstance(error, ProxboxException):
            raise
        raise ProxboxException(message=error_msg, detail=str(error), python_exception=str(error))

    successful_devices = 0
    failed_devices = 0
    processed_count = 0

    for device in all_devices:
        device_name = str(device.get("name") or "")
        cluster_name = str(device.get("cluster") or "")
        processed_count += 1

        if bridge:
            await bridge.emit_item_progress(
                phase="devices",
                item={"name": device_name, "type": "node", "cluster": cluster_name},
                operation=ItemOperation.CREATED,
                status="processing",
                message=f"Finalizing device '{device_name}'",
                progress_current=processed_count,
                progress_total=len(all_devices),
            )

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "device",
                    "type": "create",
                    "data": {
                        "completed": False,
                        "sync_status": return_status_html("syncing", use_css),
                        "rowid": device_name,
                        "name": device_name,
                        "netbox_id": None,
                    },
                }
            )

        record = devices_by_name.get(device_name)
        if record is None:
            failed_devices += 1
            if bridge:
                await bridge.emit_item_progress(
                    phase="devices",
                    item={"name": device_name, "type": "node", "cluster": cluster_name},
                    operation=ItemOperation.FAILED,
                    status="failed",
                    message=f"Failed to sync device '{device_name}'",
                    progress_current=processed_count,
                    progress_total=len(all_devices),
                    error="Device missing from bulk reconcile result",
                )
            continue

        data = record.serialize()
        if node and node == device_name:
            return [data]

        device_list.append(data)
        successful_devices += 1

        if bridge:
            await bridge.emit_item_progress(
                phase="devices",
                item={
                    "name": device_name,
                    "type": "node",
                    "cluster": cluster_name,
                    "netbox_id": data.get("id"),
                    "netbox_url": data.get("display_url"),
                },
                operation=ItemOperation.CREATED,
                status="completed",
                message=f"Synced device '{device_name}'",
                progress_current=processed_count,
                progress_total=len(all_devices),
            )

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "device",
                    "type": "create",
                    "data": {
                        "completed": True,
                        "increment_count": "yes",
                        "sync_status": return_status_html("completed", use_css),
                        "rowid": device_name,
                        "name": f"<a href='{data.get('display_url')}'>{data.get('name')}</a>",
                        "netbox_id": data.get("id"),
                        "role": f"<a href='{(data.get('role') or {}).get('url')}'>{(data.get('role') or {}).get('name')}</a>",
                        "cluster": f"<a href='{(data.get('cluster') or {}).get('url')}'>{(data.get('cluster') or {}).get('name')}</a>",
                        "device_type": f"<a href='{(data.get('device_type') or {}).get('url')}'>{(data.get('device_type') or {}).get('model')}</a>",
                    },
                }
            )

    if bridge:
        await bridge.emit_phase_summary(
            phase="devices",
            created=successful_devices,
            updated=0,
            deleted=0,
            failed=failed_devices,
            skipped=0,
            message=f"Device sync completed: {successful_devices} created, {failed_devices} failed",
        )

    if use_websocket and websocket:
        await websocket.send_json({"object": "device", "end": True})

    try:
        from proxbox_api.services.hardware_discovery import is_enabled, run_for_nodes

        if is_enabled():
            hw_nodes: list[dict[str, object]] = []
            for entry in device_list:
                netbox_id = entry.get("id")
                if netbox_id is None:
                    continue
                primary = entry.get("primary_ip4") or entry.get("primary_ip")
                host = ""
                if isinstance(primary, dict):
                    raw = primary.get("address") or primary.get("display") or ""
                    host = str(raw).split("/", 1)[0].strip()
                elif isinstance(primary, str):
                    host = primary.split("/", 1)[0].strip()
                hw_nodes.append(
                    {
                        "id": netbox_id,
                        "name": entry.get("name"),
                        "host": host,
                        "cluster": device_cluster_map.get(str(entry.get("name") or "")),
                    }
                )
            if hw_nodes:
                await run_for_nodes(nb, hw_nodes, bridge=bridge)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Hardware discovery pass failed: %s", exc)

    global_cache.clear_cache()
    return device_list


ProxmoxCreateDevicesDep = Annotated[list[dict], Depends(create_proxmox_devices)]
