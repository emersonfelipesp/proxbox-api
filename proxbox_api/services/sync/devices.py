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
    _effective_cluster_site_id,
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
from proxbox_api.services.sync.node_device_name import NodeDeviceNameError, render_node_device_name
from proxbox_api.utils import return_status_html
from proxbox_api.utils.streaming import WebSocketSSEBridge
from proxbox_api.utils.structured_logging import SyncPhaseLogger

__all__ = [
    "_effective_cluster_site_id",
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


def _cluster_device_items(
    cluster_status: object,
) -> tuple[list[dict[str, object]], list[NodeDeviceNameError]]:
    cluster_name = str(getattr(cluster_status, "name", "") or "").strip()
    cluster_mode = str(getattr(cluster_status, "mode", "") or "").strip().capitalize()
    template = str(getattr(cluster_status, "node_device_name_template", "{node}") or "{node}")
    endpoint_name = str(getattr(cluster_status, "endpoint_name", "") or cluster_name)
    items: list[dict[str, object]] = []
    errors: list[NodeDeviceNameError] = []
    for node_obj in getattr(cluster_status, "node_list", None) or []:
        node_name = str(getattr(node_obj, "name", "") or "").strip()
        if not node_name:
            continue
        try:
            device_name = render_node_device_name(node_name, cluster_name, endpoint_name, template)
        except NodeDeviceNameError as error:
            errors.append(error)
            continue
        items.append(
            {
                "name": device_name,
                "node": node_name,
                "type": "node",
                "cluster": cluster_name,
                "cluster_mode": cluster_mode or "Proxmox",
                "endpoint_id": getattr(cluster_status, "db_endpoint_id", None),
            }
        )
    return items, errors


def _discover_device_items(
    clusters_status: list[object],
) -> tuple[list[dict[str, object]], dict[str, str], list[NodeDeviceNameError]]:
    discovered = [_cluster_device_items(cluster_status) for cluster_status in clusters_status]
    items = [item for cluster_items, _errors in discovered for item in cluster_items]
    errors = [error for _items, cluster_errors in discovered for error in cluster_errors]
    cluster_by_device_name = {str(item["name"]): str(item["cluster"]) for item in items}
    return items, cluster_by_device_name, errors


async def _report_device_name_errors(
    errors: list[NodeDeviceNameError],
    bridge: WebSocketSSEBridge | None,
) -> None:
    total = len(errors)
    for current, error in enumerate(errors, start=1):
        message = str(error)
        logger.error("Skipping Proxmox node device sync: %s", message)
        if bridge:
            await bridge.emit_item_progress(
                phase="devices",
                item={"name": error.node, "type": "node", "cluster": error.cluster},
                operation=ItemOperation.FAILED,
                status="failed",
                message=f"Skipped device sync for node {error.node!r}",
                progress_current=current,
                progress_total=total,
                error=message,
            )


async def _reconcile_devices_or_raise(
    nb: object,
    *,
    clusters_status: list[object],
    tag_refs: list[dict[str, object]],
    overwrite_device_role: bool,
    overwrite_device_type: bool,
    overwrite_device_tags: bool,
    overwrite_flags: SyncOverwriteFlags | None,
    bridge: WebSocketSSEBridge | None,
) -> dict[tuple[int | None, str, str], object]:
    try:
        return await ensure_proxmox_devices_bulk(
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
        raise ProxboxException(
            message=error_msg,
            detail=str(error),
            python_exception=str(error),
        ) from error


async def _emit_device_websocket_start(
    websocket: WebSocketSSEBridge | None,
    *,
    enabled: bool,
    device_name: str,
    use_css: bool,
) -> None:
    if not enabled or websocket is None:
        return
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


async def _emit_device_websocket_complete(
    websocket: WebSocketSSEBridge | None,
    *,
    enabled: bool,
    device_name: str,
    data: dict[str, object],
    use_css: bool,
) -> None:
    if not enabled or websocket is None:
        return
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


async def _emit_missing_device(
    bridge: WebSocketSSEBridge | None,
    *,
    device_name: str,
    cluster_name: str,
    processed_count: int,
    total: int,
) -> None:
    if bridge is None:
        return
    await bridge.emit_item_progress(
        phase="devices",
        item={"name": device_name, "type": "node", "cluster": cluster_name},
        operation=ItemOperation.FAILED,
        status="failed",
        message=f"Failed to sync device '{device_name}'",
        progress_current=processed_count,
        progress_total=total,
        error="Device missing from bulk reconcile result",
    )


async def _finalize_devices(
    items: list[dict[str, object]],
    records: dict[tuple[str, str], object],
    *,
    bridge: WebSocketSSEBridge | None,
    websocket: WebSocketSSEBridge | None,
    use_websocket: bool,
    use_css: bool,
    selected_node: str | None,
) -> tuple[list[dict[str, object]], int]:
    finalized: list[dict[str, object]] = []
    failed = 0
    for processed_count, item in enumerate(items, start=1):
        device_name = str(item.get("name") or "")
        node_name = str(item.get("node") or "")
        cluster_name = str(item.get("cluster") or "")
        if bridge:
            await bridge.emit_item_progress(
                phase="devices",
                item={"name": device_name, "type": "node", "cluster": cluster_name},
                operation=ItemOperation.CREATED,
                status="processing",
                message=f"Finalizing device '{device_name}'",
                progress_current=processed_count,
                progress_total=len(items),
            )
        await _emit_device_websocket_start(
            websocket,
            enabled=use_websocket,
            device_name=device_name,
            use_css=use_css,
        )
        endpoint_id = item.get("endpoint_id")
        endpoint_key = endpoint_id if isinstance(endpoint_id, int) else None
        record = records.get((endpoint_key, cluster_name, node_name))
        if record is None:
            failed += 1
            await _emit_missing_device(
                bridge,
                device_name=device_name,
                cluster_name=cluster_name,
                processed_count=processed_count,
                total=len(items),
            )
            continue
        data = record.serialize()
        if selected_node and selected_node == node_name:
            return [data], failed
        finalized.append(data)
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
                progress_total=len(items),
            )
        await _emit_device_websocket_complete(
            websocket,
            enabled=use_websocket,
            device_name=device_name,
            data=data,
            use_css=use_css,
        )
    return finalized, failed


def _hardware_node_payload(
    entry: dict[str, object],
    cluster_by_device_name: dict[str, str],
) -> dict[str, object] | None:
    netbox_id = entry.get("id")
    if netbox_id is None:
        return None
    primary = entry.get("primary_ip4") or entry.get("primary_ip")
    raw_host = (
        primary.get("address") or primary.get("display") if isinstance(primary, dict) else primary
    )
    device_name = str(entry.get("name") or "")
    return {
        "id": netbox_id,
        "name": device_name,
        "host": str(raw_host or "").split("/", 1)[0].strip(),
        "cluster": cluster_by_device_name.get(device_name),
    }


async def _run_hardware_discovery(
    nb: object,
    device_list: list[dict[str, object]],
    *,
    cluster_by_device_name: dict[str, str],
    bridge: WebSocketSSEBridge | None,
    tag_refs: list[dict[str, object]],
) -> None:
    try:
        from proxbox_api.services.hardware_discovery import is_enabled, run_for_nodes

        if not is_enabled():
            return
        hw_nodes = [
            payload
            for entry in device_list
            if (payload := _hardware_node_payload(entry, cluster_by_device_name)) is not None
        ]
        if hw_nodes:
            await run_for_nodes(nb, hw_nodes, bridge=bridge, tag_refs=tag_refs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Hardware discovery pass failed: %s", exc)


async def create_proxmox_devices(
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
    phase_logger = SyncPhaseLogger("device_sync", cluster_mode="proxmox")
    if not clusters_status:
        phase_logger.log_phase(
            "validation",
            "No cluster status data provided",
            level="warning",
        )
        return []

    tag_refs = nested_tag_payload(tag)
    bridge = websocket if use_websocket and isinstance(websocket, WebSocketSSEBridge) else None
    items, cluster_by_device_name, name_errors = _discover_device_items(clusters_status)
    await _report_device_name_errors(name_errors, bridge)
    if bridge:
        await bridge.emit_discovery(
            phase="devices",
            items=items,
            message=f"Discovered {len(items)} device(s) to synchronize",
            metadata={"total_devices": len(items)},
        )

    phase_logger.log_phase(
        "bulk_prerequisites",
        "Reconciling device dependency phases",
    )
    records = await _reconcile_devices_or_raise(
        netbox_session,
        clusters_status=clusters_status,
        tag_refs=tag_refs,
        overwrite_device_role=overwrite_device_role,
        overwrite_device_type=overwrite_device_type,
        overwrite_device_tags=overwrite_device_tags,
        overwrite_flags=overwrite_flags,
        bridge=bridge,
    )
    device_list, failed_devices = await _finalize_devices(
        items,
        records,
        bridge=bridge,
        websocket=websocket,
        use_websocket=use_websocket,
        use_css=use_css,
        selected_node=node,
    )
    failed_devices += len(name_errors)
    if node and device_list:
        return device_list

    if bridge:
        await bridge.emit_phase_summary(
            phase="devices",
            created=len(device_list),
            updated=0,
            deleted=0,
            failed=failed_devices,
            skipped=0,
            message=(f"Device sync completed: {len(device_list)} created, {failed_devices} failed"),
        )
    if use_websocket and websocket:
        await websocket.send_json({"object": "device", "end": True})

    await _run_hardware_discovery(
        netbox_session,
        device_list,
        cluster_by_device_name=cluster_by_device_name,
        bridge=bridge,
        tag_refs=tag_refs,
    )
    global_cache.clear_cache()
    return device_list


ProxmoxCreateDevicesDep = Annotated[list[dict], Depends(create_proxmox_devices)]
