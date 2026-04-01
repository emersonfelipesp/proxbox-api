"""Device synchronization service from Proxmox nodes to NetBox."""

from typing import Annotated, Any

from fastapi import Depends

from proxbox_api.cache import global_cache
from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import nested_tag_payload
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


async def create_proxmox_devices(
    netbox_session: Any,
    clusters_status: list[Any] | None,
    tag: ProxboxTagDep,
    websocket: Any | None = None,
    node: str | None = None,
    use_websocket: bool = False,
    use_css: bool = False,
) -> list[dict[str, Any]]:
    """Create and synchronize devices from Proxmox cluster nodes to NetBox.

    This function iterates through cluster status objects, extracts node information,
    and creates corresponding NetBox device records with appropriate metadata.

    Args:
        netbox_session: NetBox API session for creating/updating devices.
        clusters_status: List of cluster status objects containing node information.
        tag: ProxBox tag reference for tagging created objects.
        websocket: Optional WebSocket connection for streaming progress updates.
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

    logger.info("Device Sync Process Started")

    device_list: list[dict[str, Any]] = []

    if not clusters_status:
        logger.info("No cluster status data provided for device sync")
        return device_list

    try:
        # Count total devices to process (just for journalling)
        for cluster_status in clusters_status:
            if cluster_status and cluster_status.node_list:
                device_count = len(cluster_status.node_list)
                total_devices += device_count

        for cluster_status in clusters_status:
            if not cluster_status or not cluster_status.node_list:
                continue

            logger.info(f"🔄 Processing Cluster: {cluster_status.name}")

            for node_obj in cluster_status.node_list:
                device_name = node_obj.name

                logger.info(f"🔄 Processing Device: {device_name}")

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
                    try:
                        cluster_type = await _ensure_cluster_type(
                            nb,
                            mode=cluster_status.mode,
                            tag_refs=tag_refs,
                        )
                    except Exception as error:
                        raise _wrap_device_phase_error("cluster type", error) from error

                    try:
                        cluster = await _ensure_cluster(
                            nb,
                            cluster_name=cluster_status.name,
                            cluster_type_id=getattr(cluster_type, "id", None),
                            mode=cluster_status.mode,
                            tag_refs=tag_refs,
                        )
                    except Exception as error:
                        raise _wrap_device_phase_error("cluster", error) from error

                    try:
                        manufacturer = await _ensure_manufacturer(nb, tag_refs=tag_refs)
                    except Exception as error:
                        raise _wrap_device_phase_error("manufacturer", error) from error

                    try:
                        device_type = await _ensure_device_type(
                            nb,
                            manufacturer_id=getattr(manufacturer, "id", None),
                            tag_refs=tag_refs,
                        )
                    except Exception as error:
                        raise _wrap_device_phase_error("device type", error) from error

                    try:
                        role = await _ensure_device_role(nb, tag_refs=tag_refs)
                    except Exception as error:
                        raise _wrap_device_phase_error("device role", error) from error

                    try:
                        site = await _ensure_site(
                            nb,
                            cluster_name=cluster_status.name,
                            tag_refs=tag_refs,
                        )
                    except Exception as error:
                        raise _wrap_device_phase_error("site", error) from error

                    netbox_device = None

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
                        except Exception as error:
                            raise _wrap_device_phase_error("device", error) from error

                        logger.info(f"✅ Device created/synced successfully: {device_name}")

                    if netbox_device:
                        netbox_device_data = netbox_device.json

                        # If node, return only the node requested.
                        if node and node == device_name:
                            return [netbox_device_data] if netbox_device_data else []

                        device_list.append(netbox_device_data)
                        successful_devices += 1

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
                                        #'manufacturer': f"<a href='{netbox_device.get('manufacturer').get('url')}'>{netbox_device.get('manufacturer').get('name')}</a>",
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

                        if use_websocket and websocket:
                            # Handle the case where netbox_device is None
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

                except Exception as error:
                    failed_devices += 1
                    error_msg = f"Error creating device {device_name}: {str(error)}"
                    if isinstance(error, ProxboxException):
                        raise error
                    raise ProxboxException(
                        message="Error creating NetBox device",
                        detail=str(error),
                        python_exception=str(error),
                    )

        # Send end message to websocket to indicate that the creation of devices is finished.
        if all([use_websocket, websocket]):
            await websocket.send_json({"object": "device", "end": True})

        # Clear cache after creating devices.
        global_cache.clear_cache()

    except ProxboxException as error:
        error_msg = f"Error during device sync: {error.message}"
        logger.error(error_msg)
        raise ProxboxException(
            message=error_msg,
            detail=error.detail,
            python_exception=error.python_exception,
        )
    except Exception as error:
        error_msg = f"Error during device sync: {str(error)}"
        logger.error(error_msg)
        raise ProxboxException(message=error_msg, detail=str(error), python_exception=str(error))

    return device_list


ProxmoxCreateDevicesDep = Annotated[list[dict], Depends(create_proxmox_devices)]
