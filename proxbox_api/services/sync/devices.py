"""Device synchronization service from Proxmox nodes to NetBox."""

import asyncio
import re
from datetime import datetime
from typing import Annotated

from fastapi import Depends

from proxbox_api.cache import global_cache
from proxbox_api.dependencies import ProxboxTagDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    nested_tag_payload,
    rest_create_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxClusterSyncState,
    NetBoxClusterTypeSyncState,
    NetBoxDeviceRoleSyncState,
    NetBoxDeviceSyncState,
    NetBoxDeviceTypeSyncState,
    NetBoxManufacturerSyncState,
    NetBoxSiteSyncState,
)
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep
from proxbox_api.session.netbox import NetBoxSessionDep
from proxbox_api.utils import return_status_html, sync_process


def _slugify(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "cluster"


async def _ensure_cluster_type(nb, *, mode: str, tag_refs: list[dict]) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/virtualization/cluster-types/",
        lookup={"slug": mode},
        payload={
            "name": mode.capitalize(),
            "slug": mode,
            "description": f"Proxmox {mode} mode",
            "tags": tag_refs,
        },
        schema=NetBoxClusterTypeSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "description": record.get("description"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_cluster(
    nb, *, cluster_name: str, cluster_type_id: int | None, mode: str, tag_refs
):
    return await rest_reconcile_async(
        nb,
        "/api/virtualization/clusters/",
        lookup={"name": cluster_name},
        payload={
            "name": cluster_name,
            "type": cluster_type_id,
            "description": f"Proxmox {mode} cluster.",
            "tags": tag_refs,
        },
        schema=NetBoxClusterSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "type": record.get("type"),
            "description": record.get("description"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_manufacturer(nb, *, tag_refs: list[dict]) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/dcim/manufacturers/",
        lookup={"slug": "proxmox"},
        payload={
            "name": "Proxmox",
            "slug": "proxmox",
            "tags": tag_refs,
        },
        schema=NetBoxManufacturerSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_device_type(nb, *, manufacturer_id: int | None, tag_refs: list[dict]) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/dcim/device-types/",
        lookup={"model": "Proxmox Generic Device"},
        payload={
            "model": "Proxmox Generic Device",
            "slug": "proxmox-generic-device",
            "manufacturer": manufacturer_id,
            "tags": tag_refs,
        },
        schema=NetBoxDeviceTypeSyncState,
        current_normalizer=lambda record: {
            "model": record.get("model"),
            "slug": record.get("slug"),
            "manufacturer": record.get("manufacturer"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_device_role(nb, *, tag_refs: list[dict]) -> object:
    return await rest_reconcile_async(
        nb,
        "/api/dcim/device-roles/",
        lookup={"slug": "proxmox-node"},
        payload={
            "name": "Proxmox Node",
            "slug": "proxmox-node",
            "color": "00bcd4",
            "tags": tag_refs,
        },
        schema=NetBoxDeviceRoleSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "color": record.get("color"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_site(nb, *, cluster_name: str, tag_refs: list[dict]) -> object:
    site_name = f"Proxmox Default Site - {cluster_name}"
    site_slug = f"proxmox-default-site-{_slugify(cluster_name)}"
    return await rest_reconcile_async(
        nb,
        "/api/dcim/sites/",
        lookup={"slug": site_slug},
        payload={
            "name": site_name,
            "slug": site_slug,
            "status": "active",
            "tags": tag_refs,
        },
        schema=NetBoxSiteSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "status": record.get("status"),
            "tags": record.get("tags"),
        },
    )


async def _ensure_device(
    nb,
    *,
    device_name: str,
    cluster_id: int | None,
    device_type_id: int | None,
    role_id: int | None,
    site_id: int | None,
    tag_refs: list[dict],
) -> object:
    payload = {
        "name": device_name,
        "tags": tag_refs,
        "cluster": cluster_id,
        "status": "active",
        "description": f"Proxmox Node {device_name}",
        "device_type": device_type_id,
        "role": role_id,
        "site": site_id,
    }
    return await rest_reconcile_async(
        nb,
        "/api/dcim/devices/",
        lookup={"name": device_name, "site_id": site_id},
        payload=payload,
        schema=NetBoxDeviceSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "status": record.get("status"),
            "cluster": record.get("cluster"),
            "device_type": record.get("device_type"),
            "role": record.get("role"),
            "site": record.get("site"),
            "description": record.get("description"),
            "tags": record.get("tags"),
        },
    )


def _wrap_device_phase_error(phase: str, error: Exception) -> ProxboxException:
    if isinstance(error, ProxboxException):
        return ProxboxException(
            message=f"Error creating NetBox {phase}",
            detail=error.detail or error.message,
            python_exception=error.python_exception,
        )
    return ProxboxException(
        message=f"Error creating NetBox {phase}",
        detail=str(error),
        python_exception=str(error),
    )


@sync_process(sync_type="devices")
async def create_proxmox_devices(
    netbox_session: NetBoxSessionDep,
    clusters_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    websocket=None,
    node: str | None = None,
    use_websocket: bool = False,
    use_css: bool = False,
    sync_process=None,
):
    tag_refs = nested_tag_payload(tag)

    # GET /api/plugins/proxbox/sync-processes/
    nb = netbox_session
    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    journal_messages = []  # Store all journal messages
    total_devices = 0  # Track total devices processed
    successful_devices = 0  # Track successful device creations
    failed_devices = 0  # Track failed device creations

    journal_messages.append("## Device Sync Process Started")
    logger.info("Device Sync Process Started")
    journal_messages.append(f"- **Start Time**: {start_time}")
    journal_messages.append("- **Status**: Initializing")

    device_list: list = []

    try:
        journal_messages.append("\n## Device Discovery")

        # Count total devices to process (just for journalling)
        for cluster_status in clusters_status:
            if cluster_status and cluster_status.node_list:
                device_count = len(cluster_status.node_list)
                total_devices += device_count

                journal_messages.append(
                    f"- Cluster `{cluster_status.name}` ({cluster_status.mode}): Found {device_count} devices"
                )

        journal_messages.append("\n## Device Processing")
        journal_messages.append(f"- Total devices to process: {total_devices}")

        for cluster_status in clusters_status:
            if not cluster_status or not cluster_status.node_list:
                continue

            journal_messages.append(f"\n### 🔄Processing Cluster: {cluster_status.name}")
            logger.info(f"🔄 Processing Cluster: {cluster_status.name}")

            journal_messages.append(f"- Cluster Mode: {cluster_status.mode}")
            journal_messages.append(f"- Devices in cluster: {len(cluster_status.node_list)}")

            for node_obj in cluster_status.node_list:
                device_name = node_obj.name

                journal_messages.append(f"\n#### 🔄 Processing Device: {device_name}")
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
                    journal_messages.append(
                        f"- Creating cluster type: {cluster_status.mode.capitalize()}"
                    )
                    try:
                        cluster_type = await _ensure_cluster_type(
                            nb,
                            mode=cluster_status.mode,
                            tag_refs=tag_refs,
                        )
                    except Exception as error:
                        raise _wrap_device_phase_error("cluster type", error) from error

                    journal_messages.append(f"- Creating cluster: {cluster_status.name}")
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

                    journal_messages.append(
                        "- Creating device type, role, manufacturer, and site placeholders"
                    )
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
                        journal_messages.append(f"- Creating device: {device_name}")
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

                        journal_messages.append(
                            f"- ✅ Device created/synced successfully: {device_name}"
                        )
                        logger.info(f"✅ Device created/synced successfully: {device_name}")

                    if netbox_device:
                        netbox_device_data = netbox_device.json

                        # If node, return only the node requested.
                        if node and node == device_name:
                            journal_messages.append(f"- Returning single device: {device_name}")

                            return [netbox_device_data] if netbox_device_data else []

                        device_list.append(netbox_device_data)
                        successful_devices += 1
                        journal_messages.append(
                            f"- ✅ Successfully created device: {device_name} (ID: {netbox_device_data.get('id')})"
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
                        journal_messages.append(f"- ❌ {error_msg}")

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
                    journal_messages.append(f"- ❌ {error_msg}")
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

        journal_messages.append("\n## Process Summary")
        journal_messages.append(f"- **Status**: {getattr(sync_process, 'status', 'unknown')}")
        journal_messages.append(
            f"- **Runtime**: {getattr(sync_process, 'runtime', 'unknown')} seconds"
        )
        journal_messages.append(f"- **End Time**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        journal_messages.append(f"- **Total Devices Processed**: {total_devices}")
        journal_messages.append(f"- **Successfully Created**: {successful_devices}")
        journal_messages.append(f"- **Failed**: {failed_devices}")

    except ProxboxException as error:
        error_msg = f"Error during device sync: {error.message}"
        journal_messages.append(f"\n### ❌ Error\n{error_msg}")
        raise ProxboxException(
            message=error_msg,
            detail=error.detail,
            python_exception=error.python_exception,
        )
    except Exception as error:
        error_msg = f"Error during device sync: {str(error)}"
        journal_messages.append(f"\n### ❌ Error\n{error_msg}")
        raise ProxboxException(message=error_msg, detail=str(error), python_exception=str(error))

    finally:
        # Create journal entry
        try:
            if sync_process and hasattr(sync_process, "id"):
                journal_entry = await rest_create_async(
                    nb,
                    "/api/extras/journal-entries/",
                    {
                        "assigned_object_type": "netbox_proxbox.syncprocess",
                        "assigned_object_id": sync_process.id,
                        "kind": "info",
                        "comments": "\n".join(journal_messages),
                        "tags": tag_refs,
                    },
                )

                if not journal_entry:
                    print("Warning: Journal entry creation returned None")
            else:
                print("Warning: Cannot create journal entry - sync_process is None or has no id")
        except Exception as journal_error:
            print(f"Warning: Failed to create journal entry: {str(journal_error)}")

    return device_list


ProxmoxCreateDevicesDep = Annotated[list[dict], Depends(create_proxmox_devices)]
