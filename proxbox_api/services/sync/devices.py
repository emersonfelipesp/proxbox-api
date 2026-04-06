"""Device synchronization service from Proxmox nodes to NetBox."""

from typing import Annotated

from fastapi import Depends

from proxbox_api.cache import global_cache
from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import nested_tag_payload
from proxbox_api.schemas.stream_messages import ErrorCategory, ItemOperation, SubstepStatus
from proxbox_api.services.sync.device_ensure import (
    _ensure_cluster,
    _ensure_cluster_type,
    _ensure_device,
    _ensure_device_role,
    _ensure_device_type,
    _ensure_manufacturer,
    _ensure_site,
    _wrap_device_phase_error,
)
from proxbox_api.utils import return_status_html
from proxbox_api.utils.streaming import WebSocketSSEBridge
from proxbox_api.utils.structured_logging import SyncPhaseLogger


async def create_proxmox_devices(  # noqa: C901
    netbox_session: object,
    clusters_status: list[object] | None,
    tag: ProxboxTagDep,
    websocket: object | None = None,
    node: str | None = None,
    use_websocket: bool = False,
    use_css: bool = False,
) -> list[dict[str, object]]:
    """Create and synchronize devices from Proxmox cluster nodes to NetBox.

    This function iterates through cluster status objects, extracts node information,
    and creates corresponding NetBox device records with appropriate metadata.

    Args:
        netbox_session: NetBox API session for creating/updating devices.
        clusters_status: List of cluster status objects containing node information.
        tag: ProxBox tag reference for tagging created objects.
        websocket: Optional WebSocket/SSE bridge connection for streaming progress updates.
        node: Optional specific node name to filter processing.
        use_websocket: Whether to send progress updates via WebSocket/SSE.
        use_css: Whether to include CSS styling in HTML status responses.

    Returns:
        List of created/synced device records from NetBox as dictionaries.

    Raises:
        ProxboxException: If device creation or synchronization fails.
    """
    tag_refs = nested_tag_payload(tag)
    nb = netbox_session

    total_devices = 0  # Track total devices processed
    successful_devices = 0  # Track successful device creations
    failed_devices = 0  # Track failed device creations

    # Initialize structured logger
    phase_logger = SyncPhaseLogger("device_sync", cluster_mode="proxmox")
    phase_logger.log_phase("initialization", "Device sync process starting")

    device_list: list[dict[str, object]] = []

    if not clusters_status:
        phase_logger.log_phase("validation", "No cluster status data provided", level="warning")
        return device_list

    # Bridge helper for new emission methods (if websocket is WebSocketSSEBridge)
    bridge: WebSocketSSEBridge | None = None
    if use_websocket and isinstance(websocket, WebSocketSSEBridge):
        bridge = websocket

    try:
        # Collect all devices to process for discovery message
        all_devices: list[dict[str, object]] = []
        for cluster_status in clusters_status:
            if cluster_status and cluster_status.node_list:
                for node_obj in cluster_status.node_list:
                    all_devices.append(
                        {
                            "name": node_obj.name,
                            "type": "node",
                            "cluster": cluster_status.name,
                            "cluster_mode": cluster_status.mode.capitalize()
                            if hasattr(cluster_status, "mode")
                            else "Proxmox",
                        }
                    )

        # Count total devices to process
        total_devices = len(all_devices)

        # Emit discovery message with all devices to be processed
        if bridge:
            await bridge.emit_discovery(
                phase="devices",
                items=all_devices,
                message=f"Discovered {total_devices} device(s) to synchronize",
                metadata={"total_devices": total_devices},
            )

        # Track progress for item_progress messages
        processed_count = 0

        for cluster_status in clusters_status:
            if not cluster_status or not cluster_status.node_list:
                continue

            cluster_logger = SyncPhaseLogger("device_sync", cluster=cluster_status.name)
            cluster_logger.log_phase("processing", f"Processing cluster: {cluster_status.name}")

            for node_obj in cluster_status.node_list:
                device_name = node_obj.name
                device_logger = SyncPhaseLogger(
                    "device_sync",
                    cluster=cluster_status.name,
                    device=device_name,
                )
                device_logger.log_phase("processing", f"Processing device: {device_name}")
                processed_count += 1

                # Start timing for this device
                timing_key = f"device_{device_name}"
                if bridge:
                    bridge.start_timer(timing_key)

                # Emit item started message
                if bridge:
                    await bridge.emit_item_progress(
                        phase="devices",
                        item={"name": device_name, "type": "node", "cluster": cluster_status.name},
                        operation=ItemOperation.CREATED,
                        status="processing",
                        message=f"Processing device '{device_name}'",
                        progress_current=processed_count,
                        progress_total=total_devices,
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
                                "manufacturer": None,
                                "role": None,
                                "cluster": cluster_status.mode.capitalize(),
                                "device_type": None,
                            },
                        }
                    )

                try:
                    # Substep: ensure cluster type
                    if bridge:
                        await bridge.emit_substep(
                            phase="devices",
                            substep="ensure_cluster_type",
                            status=SubstepStatus.PROCESSING,
                            message=f"Ensuring cluster type '{cluster_status.mode.capitalize()}'",
                            item={"name": device_name},
                        )
                    try:
                        cluster_type = await _ensure_cluster_type(
                            nb,
                            mode=cluster_status.mode,
                            tag_refs=tag_refs,
                        )
                        if bridge:
                            await bridge.emit_substep(
                                phase="devices",
                                substep="ensure_cluster_type",
                                status=SubstepStatus.COMPLETED,
                                message=f"Cluster type '{cluster_status.mode.capitalize()}' ready",
                                item={
                                    "name": device_name,
                                    "netbox_id": getattr(cluster_type, "id", None),
                                },
                            )
                    except Exception as error:
                        if bridge:
                            await bridge.emit_error_detail(
                                message="Failed to ensure cluster type",
                                category=ErrorCategory.VALIDATION,
                                phase="devices",
                                item={"name": device_name},
                                detail=str(error),
                                suggestion="Check NetBox permissions and cluster type configuration",
                            )
                        raise _wrap_device_phase_error("cluster type", error) from error

                    # Substep: ensure cluster
                    if bridge:
                        await bridge.emit_substep(
                            phase="devices",
                            substep="ensure_cluster",
                            status=SubstepStatus.PROCESSING,
                            message=f"Ensuring cluster '{cluster_status.name}'",
                            item={"name": device_name},
                        )
                    try:
                        cluster = await _ensure_cluster(
                            nb,
                            cluster_name=cluster_status.name,
                            cluster_type_id=getattr(cluster_type, "id", None),
                            mode=cluster_status.mode,
                            tag_refs=tag_refs,
                        )
                        if bridge:
                            await bridge.emit_substep(
                                phase="devices",
                                substep="ensure_cluster",
                                status=SubstepStatus.COMPLETED,
                                message=f"Cluster '{cluster_status.name}' ready",
                                item={
                                    "name": device_name,
                                    "netbox_id": getattr(cluster, "id", None),
                                },
                            )
                    except Exception as error:
                        if bridge:
                            await bridge.emit_error_detail(
                                message="Failed to ensure cluster",
                                category=ErrorCategory.VALIDATION,
                                phase="devices",
                                item={"name": device_name},
                                detail=str(error),
                                suggestion="Check NetBox permissions and cluster configuration",
                            )
                        raise _wrap_device_phase_error("cluster", error) from error

                    # Substep: ensure manufacturer
                    if bridge:
                        await bridge.emit_substep(
                            phase="devices",
                            substep="ensure_manufacturer",
                            status=SubstepStatus.PROCESSING,
                            message="Ensuring manufacturer",
                            item={"name": device_name},
                        )
                    try:
                        manufacturer = await _ensure_manufacturer(nb, tag_refs=tag_refs)
                        if bridge:
                            await bridge.emit_substep(
                                phase="devices",
                                substep="ensure_manufacturer",
                                status=SubstepStatus.COMPLETED,
                                message="Manufacturer ready",
                                item={"name": device_name},
                            )
                    except Exception as error:
                        if bridge:
                            await bridge.emit_error_detail(
                                message="Failed to ensure manufacturer",
                                category=ErrorCategory.VALIDATION,
                                phase="devices",
                                item={"name": device_name},
                                detail=str(error),
                                suggestion="Check NetBox permissions and manufacturer configuration",
                            )
                        raise _wrap_device_phase_error("manufacturer", error) from error

                    # Substep: ensure device type
                    if bridge:
                        await bridge.emit_substep(
                            phase="devices",
                            substep="ensure_device_type",
                            status=SubstepStatus.PROCESSING,
                            message="Ensuring device type",
                            item={"name": device_name},
                        )
                    try:
                        device_type = await _ensure_device_type(
                            nb,
                            manufacturer_id=getattr(manufacturer, "id", None),
                            tag_refs=tag_refs,
                        )
                        if bridge:
                            await bridge.emit_substep(
                                phase="devices",
                                substep="ensure_device_type",
                                status=SubstepStatus.COMPLETED,
                                message="Device type ready",
                                item={"name": device_name},
                            )
                    except Exception as error:
                        if bridge:
                            await bridge.emit_error_detail(
                                message="Failed to ensure device type",
                                category=ErrorCategory.VALIDATION,
                                phase="devices",
                                item={"name": device_name},
                                detail=str(error),
                                suggestion="Check NetBox permissions and device type configuration",
                            )
                        raise _wrap_device_phase_error("device type", error) from error

                    # Substep: ensure device role
                    if bridge:
                        await bridge.emit_substep(
                            phase="devices",
                            substep="ensure_device_role",
                            status=SubstepStatus.PROCESSING,
                            message="Ensuring device role",
                            item={"name": device_name},
                        )
                    try:
                        role = await _ensure_device_role(nb, tag_refs=tag_refs)
                        if bridge:
                            await bridge.emit_substep(
                                phase="devices",
                                substep="ensure_device_role",
                                status=SubstepStatus.COMPLETED,
                                message="Device role ready",
                                item={"name": device_name},
                            )
                    except Exception as error:
                        if bridge:
                            await bridge.emit_error_detail(
                                message="Failed to ensure device role",
                                category=ErrorCategory.VALIDATION,
                                phase="devices",
                                item={"name": device_name},
                                detail=str(error),
                                suggestion="Check NetBox permissions and device role configuration",
                            )
                        raise _wrap_device_phase_error("device role", error) from error

                    # Substep: ensure site
                    if bridge:
                        await bridge.emit_substep(
                            phase="devices",
                            substep="ensure_site",
                            status=SubstepStatus.PROCESSING,
                            message=f"Ensuring site for cluster '{cluster_status.name}'",
                            item={"name": device_name},
                        )
                    try:
                        site = await _ensure_site(
                            nb,
                            cluster_name=cluster_status.name,
                            tag_refs=tag_refs,
                        )
                        if bridge:
                            await bridge.emit_substep(
                                phase="devices",
                                substep="ensure_site",
                                status=SubstepStatus.COMPLETED,
                                message=f"Site ready for cluster '{cluster_status.name}'",
                                item={"name": device_name},
                            )
                    except Exception as error:
                        if bridge:
                            await bridge.emit_error_detail(
                                message="Failed to ensure site",
                                category=ErrorCategory.VALIDATION,
                                phase="devices",
                                item={"name": device_name},
                                detail=str(error),
                                suggestion="Check NetBox permissions and site configuration",
                            )
                        raise _wrap_device_phase_error("site", error) from error

                    netbox_device = None

                    # Substep: create device
                    if bridge:
                        await bridge.emit_substep(
                            phase="devices",
                            substep="create_device",
                            status=SubstepStatus.PROCESSING,
                            message=f"Creating device '{device_name}'",
                            item={"name": device_name},
                        )
                    if cluster is not None:
                        try:
                            netbox_device = await _ensure_device(
                                nb,
                                device_name=device_name,
                                cluster_id=getattr(cluster, "id", None),
                                device_type_id=getattr(device_type, "id", None),
                                role_id=getattr(role, "id", None),
                                site_id=getattr(site, "id", None),
                                tag_refs=tag_refs,
                            )
                            if bridge:
                                await bridge.emit_substep(
                                    phase="devices",
                                    substep="create_device",
                                    status=SubstepStatus.COMPLETED,
                                    message=f"Device '{device_name}' created/synced",
                                    item={
                                        "name": device_name,
                                        "netbox_id": getattr(netbox_device, "id", None),
                                    },
                                    timing_key=timing_key,
                                )
                        except Exception as error:
                            if bridge:
                                await bridge.emit_error_detail(
                                    message=f"Failed to create device '{device_name}'",
                                    category=ErrorCategory.INTERNAL,
                                    phase="devices",
                                    item={"name": device_name},
                                    detail=str(error),
                                    suggestion="Check NetBox permissions and device configuration",
                                )
                            raise _wrap_device_phase_error("device", error) from error

                        device_logger.log_phase_complete(
                            "creation",
                            f"Device {device_name} created/synced successfully",
                            device_id=getattr(netbox_device, "id", "unknown"),
                        )

                    if netbox_device:
                        netbox_device_data = netbox_device.json

                        # If node, return only the node requested.
                        if node and node == device_name:
                            return [netbox_device_data] if netbox_device_data else []

                        device_list.append(netbox_device_data)
                        successful_devices += 1

                        if bridge:
                            await bridge.emit_item_progress(
                                phase="devices",
                                item={
                                    "name": device_name,
                                    "type": "node",
                                    "cluster": cluster_status.name,
                                    "netbox_id": netbox_device_data.get("id"),
                                    "netbox_url": netbox_device_data.get("display_url"),
                                },
                                operation=ItemOperation.CREATED,
                                status="completed",
                                message=f"Synced device '{device_name}'",
                                progress_current=processed_count,
                                progress_total=total_devices,
                                timing_key=timing_key,
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
                                        "name": f"<a href='{netbox_device_data.get('display_url')}'>{netbox_device_data.get('name')}</a>",
                                        "netbox_id": netbox_device_data.get("id"),
                                        "role": f"<a href='{(netbox_device_data.get('role') or {}).get('url')}'>{(netbox_device_data.get('role') or {}).get('name')}</a>",
                                        "cluster": f"<a href='{(netbox_device_data.get('cluster') or {}).get('url')}'>{(netbox_device_data.get('cluster') or {}).get('name')}</a>",
                                        "device_type": f"<a href='{(netbox_device_data.get('device_type') or {}).get('url')}'>{(netbox_device_data.get('device_type') or {}).get('model')}</a>",
                                    },
                                }
                            )
                    else:
                        failed_devices += 1
                        error_msg = (
                            f"Device creation failed for {device_name}. netbox_device is None."
                        )
                        device_logger.log_phase(
                            "creation",
                            error_msg,
                            level="error",
                        )

                        if bridge:
                            await bridge.emit_item_progress(
                                phase="devices",
                                item={"name": device_name, "type": "node"},
                                operation=ItemOperation.FAILED,
                                status="failed",
                                message=f"Failed to sync device '{device_name}'",
                                progress_current=processed_count,
                                progress_total=total_devices,
                                timing_key=timing_key,
                                error=error_msg,
                            )

                        if use_websocket and websocket:
                            await websocket.send_json(
                                {
                                    "object": "device",
                                    "type": "create",
                                    "data": {
                                        "completed": False,
                                        "increment_count": "no",
                                        "sync_status": return_status_html("failed", use_css),
                                        "rowid": device_name,
                                        "error": error_msg,
                                    },
                                }
                            )

                    # Clear timing
                    if bridge:
                        bridge.clear_timer(timing_key)

                except Exception as error:
                    failed_devices += 1
                    error_msg = f"Error creating device {device_name}: {str(error)}"
                    device_logger.log_error("creation", error_msg, error)

                    if bridge:
                        await bridge.emit_item_progress(
                            phase="devices",
                            item={"name": device_name, "type": "node"},
                            operation=ItemOperation.FAILED,
                            status="failed",
                            message=f"Failed to sync device '{device_name}'",
                            progress_current=processed_count,
                            progress_total=total_devices,
                            error=str(error),
                        )
                        bridge.clear_timer(timing_key)

                    if isinstance(error, ProxboxException):
                        raise error
                    raise ProxboxException(
                        message="Error creating NetBox device",
                        detail=str(error),
                        python_exception=str(error),
                    )

        # Emit phase summary
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

        # Send end message to websocket to indicate that the creation of devices is finished.
        if all([use_websocket, websocket]):
            await websocket.send_json({"object": "device", "end": True})

        # Clear cache after creating devices.
        global_cache.clear_cache()

    except ProxboxException as error:
        error_msg = f"Error during device sync: {error.message}"
        logger.error(error_msg)
        if bridge:
            await bridge.emit_error_detail(
                message=error_msg,
                category=ErrorCategory.INTERNAL,
                phase="devices",
                detail=error.detail,
            )
        raise ProxboxException(
            message=error_msg,
            detail=error.detail,
            python_exception=error.python_exception,
        )
    except Exception as error:
        error_msg = f"Error during device sync: {str(error)}"
        logger.error(error_msg)
        if bridge:
            await bridge.emit_error_detail(
                message=error_msg,
                category=ErrorCategory.INTERNAL,
                phase="devices",
                detail=str(error),
            )
        raise ProxboxException(message=error_msg, detail=str(error), python_exception=str(error))

    return device_list


ProxmoxCreateDevicesDep = Annotated[list[dict], Depends(create_proxmox_devices)]
