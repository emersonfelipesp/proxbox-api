"""Full multi-stage synchronization HTTP and SSE endpoints."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.routes.dcim import create_all_device_interfaces
from proxbox_api.routes.extras import CreateCustomFieldsDep
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines
from proxbox_api.routes.virtualization.virtual_machines.backups_vm import (
    _create_all_virtual_machine_backups,
    create_all_virtual_machine_backups,
)
from proxbox_api.routes.virtualization.virtual_machines.disks_vm import (
    create_virtual_disks,
)
from proxbox_api.routes.virtualization.virtual_machines.snapshots_vm import (
    _create_all_virtual_machine_snapshots,
    create_all_virtual_machine_snapshots,
)
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    create_only_vm_interfaces,
    create_only_vm_ip_addresses,
)
from proxbox_api.schemas.stream_messages import ErrorCategory
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.backup_routines import sync_all_backup_routines
from proxbox_api.services.sync.devices import create_proxmox_devices
from proxbox_api.services.sync.replications import sync_all_replications
from proxbox_api.services.sync.storages import create_storages
from proxbox_api.services.sync.task_history import (
    sync_all_virtual_machine_task_histories,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event
from proxbox_api.utils.structured_logging import set_operation_id

full_update_router = APIRouter()


def _result_count(value) -> int:
    """Best-effort count for stage payloads returned by sync helpers."""
    if isinstance(value, dict):
        for key in ("count", "created", "devices_count", "virtual_machines_count"):
            raw = value.get(key)
            if isinstance(raw, int):
                return raw
        return 0
    if isinstance(value, list):
        return len(value)
    return 0


@full_update_router.get("/full-update")
async def full_update_sync(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    overwrite_flags: Annotated[SyncOverwriteFlags, Query()] = SyncOverwriteFlags(),
    fetch_max_concurrency: int | None = None,
) -> dict:
    sync_nodes: list = []
    sync_storage: list = []
    sync_vms: list = []
    sync_disks: dict = {}
    sync_task_history: dict = {}
    sync_backups: list = []
    sync_snapshots: dict = {}
    sync_node_interfaces: list = []
    sync_vm_interfaces: list = []
    sync_vm_ip_addresses: list = []

    tag_refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    tag_refs = [t for t in tag_refs if t.get("name") and t.get("slug")]

    operation_id = str(uuid.uuid4())
    set_operation_id(operation_id)
    logger.info("Starting full_update sync", extra={"operation_id": operation_id})

    try:
        sync_nodes = await create_proxmox_devices(
            netbox_session=netbox_session,
            clusters_status=cluster_status,
            node=None,
            tag=tag,
            use_websocket=False,
            overwrite_device_role=overwrite_flags.overwrite_device_role,
            overwrite_device_type=overwrite_flags.overwrite_device_type,
            overwrite_device_tags=overwrite_flags.overwrite_device_tags,
            overwrite_flags=overwrite_flags,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing nodes during full-update")
        raise ProxboxException(
            message="Error while syncing nodes.", python_exception=str(error)
        ) from error

    try:
        sync_storage = await create_storages(
            netbox_session=netbox_session,
            pxs=pxs,
            tag=tag,
            use_websocket=False,
            fetch_concurrency=fetch_max_concurrency if fetch_max_concurrency is not None else 8,
            overwrite_flags=overwrite_flags,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing storages during full-update")
        raise ProxboxException(
            message="Error while syncing storages.",
            python_exception=str(error),
        ) from error

    try:
        sync_vms = await create_virtual_machines(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            custom_fields=custom_fields,
            tag=tag,
            use_websocket=False,
            sync_vm_network=False,
            overwrite_vm_role=overwrite_flags.overwrite_vm_role,
            overwrite_vm_type=overwrite_flags.overwrite_vm_type,
            overwrite_vm_tags=overwrite_flags.overwrite_vm_tags,
            overwrite_vm_description=overwrite_flags.overwrite_vm_description,
            overwrite_vm_custom_fields=overwrite_flags.overwrite_vm_custom_fields,
            overwrite_flags=overwrite_flags,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing virtual machines during full-update")
        raise ProxboxException(
            message="Error while syncing virtual machines.",
            python_exception=str(error),
        ) from error

    try:
        sync_task_history = await sync_all_virtual_machine_task_histories(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            tag_refs=tag_refs,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing task history during full-update")
        raise ProxboxException(
            message="Error while syncing task history.",
            python_exception=str(error),
        ) from error

    try:
        sync_disks = await create_virtual_disks(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            tag=tag,
            use_websocket=False,
            use_css=False,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing virtual disks during full-update")
        raise ProxboxException(
            message="Error while syncing virtual disks.",
            python_exception=str(error),
        ) from error

    try:
        sync_backups = await create_all_virtual_machine_backups(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            tag=tag,
            delete_nonexistent_backup=True,
            fetch_max_concurrency=fetch_max_concurrency,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing backups during full-update")
        raise ProxboxException(
            message="Error while syncing backups.",
            python_exception=str(error),
        ) from error

    try:
        sync_snapshots = await create_all_virtual_machine_snapshots(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            tag=tag,
            fetch_max_concurrency=fetch_max_concurrency,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing snapshots during full-update")
        raise ProxboxException(
            message="Error while syncing snapshots.",
            python_exception=str(error),
        ) from error

    try:
        sync_node_interfaces = await create_all_device_interfaces(
            netbox_session=netbox_session,
            tag=tag,
            clusters_status=cluster_status,
            use_websocket=False,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing node interfaces during full-update")
        raise ProxboxException(
            message="Error while syncing node interfaces.",
            python_exception=str(error),
        ) from error

    try:
        sync_vm_interfaces = await create_only_vm_interfaces(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            custom_fields=custom_fields,
            tag=tag,
            use_websocket=False,
            overwrite_flags=overwrite_flags,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing VM interfaces during full-update")
        raise ProxboxException(
            message="Error while syncing VM interfaces.",
            python_exception=str(error),
        ) from error

    try:
        sync_vm_ip_addresses = await create_only_vm_ip_addresses(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            custom_fields=custom_fields,
            tag=tag,
            use_websocket=False,
            overwrite_flags=overwrite_flags,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing VM IP addresses during full-update")
        raise ProxboxException(
            message="Error while syncing VM IP addresses.",
            python_exception=str(error),
        ) from error

    try:
        sync_replications = await sync_all_replications(
            netbox_session=netbox_session,
            pxs=pxs,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing replications during full-update")
        raise ProxboxException(
            message="Error while syncing replications.",
            python_exception=str(error),
        ) from error

    try:
        sync_backup_routines = await sync_all_backup_routines(
            netbox_session=netbox_session,
            pxs=pxs,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing backup routines during full-update")
        raise ProxboxException(
            message="Error while syncing backup routines.",
            python_exception=str(error),
        ) from error

    return {
        "status": "completed",
        "devices": sync_nodes,
        "storage": sync_storage,
        "virtual_machines": sync_vms,
        "virtual_disks": sync_disks,
        "task_history": sync_task_history,
        "backups": sync_backups,
        "snapshots": sync_snapshots,
        "replications": sync_replications,
        "backup_routines": sync_backup_routines,
        "node_interfaces": sync_node_interfaces,
        "vm_interfaces": sync_vm_interfaces,
        "vm_ip_addresses": sync_vm_ip_addresses,
        "devices_count": len(sync_nodes),
        "storage_count": len(sync_storage),
        "virtual_machines_count": len(sync_vms),
        "virtual_disks_count": _result_count(sync_disks),
        "task_history_count": _result_count(sync_task_history),
        "backups_count": len(sync_backups),
        "snapshots_count": _result_count(sync_snapshots),
        "replications_count": sync_replications.get("created", 0)
        + sync_replications.get("updated", 0),
        "backup_routines_count": sync_backup_routines.get("created", 0)
        + sync_backup_routines.get("updated", 0),
        "node_interfaces_count": len(sync_node_interfaces),
        "vm_interfaces_count": len(sync_vm_interfaces),
        "vm_ip_addresses_count": len(sync_vm_ip_addresses),
    }


@full_update_router.get("/full-update/stream", response_model=None)
async def full_update_sync_stream(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    overwrite_flags: Annotated[SyncOverwriteFlags, Query()] = SyncOverwriteFlags(),
    fetch_max_concurrency: int | None = None,
) -> StreamingResponse:
    async def event_stream():  # noqa: C901
        sync_nodes: list = []
        sync_storage: list = []
        sync_vms: list = []
        sync_disks: dict = {}
        sync_task_history: dict = {}
        sync_backups: list = []
        sync_snapshots: dict = {}
        sync_node_interfaces: list = []
        sync_vm_interfaces: list = []
        sync_vm_ip_addresses: list = []
        sync_replications: dict = {}
        sync_backup_routines: dict = {}
        devices_bridge = WebSocketSSEBridge()
        storage_bridge = WebSocketSSEBridge()
        vm_bridge = WebSocketSSEBridge()
        disks_bridge = WebSocketSSEBridge()
        task_history_bridge = WebSocketSSEBridge()
        backups_bridge = WebSocketSSEBridge()
        snapshots_bridge = WebSocketSSEBridge()
        node_interfaces_bridge = WebSocketSSEBridge()
        vm_interfaces_bridge = WebSocketSSEBridge()
        vm_ip_addresses_bridge = WebSocketSSEBridge()
        replications_bridge = WebSocketSSEBridge()
        backup_routines_bridge = WebSocketSSEBridge()

        tag_refs = [
            {
                "name": getattr(tag, "name", None),
                "slug": getattr(tag, "slug", None),
                "color": getattr(tag, "color", None),
            }
        ]
        tag_refs = [t for t in tag_refs if t.get("name") and t.get("slug")]

        operation_id = str(uuid.uuid4())
        set_operation_id(operation_id)
        logger.info("Starting full_update sync (stream)", extra={"operation_id": operation_id})

        try:
            yield sse_event(
                "step",
                {
                    "step": "stream",
                    "status": "started",
                    "message": f"Full update stream connected (operation_id={operation_id})",
                },
            )
            stage_items = [
                {"name": "devices", "type": "stage"},
                {"name": "storage", "type": "stage"},
                {"name": "virtual-machines", "type": "stage"},
                {"name": "virtual-disks", "type": "stage"},
                {"name": "task-history", "type": "stage"},
                {"name": "backups", "type": "stage"},
                {"name": "snapshots", "type": "stage"},
                {"name": "replications", "type": "stage"},
                {"name": "backup-routines", "type": "stage"},
                {"name": "node-interfaces", "type": "stage"},
                {"name": "vm-interfaces", "type": "stage"},
                {"name": "vm-ip-addresses", "type": "stage"},
            ]
            yield sse_event(
                "discovery",
                {
                    "event": "discovery",
                    "phase": "full-update",
                    "status": "discovered",
                    "message": f"Discovered {len(stage_items)} sync stage(s) for full update",
                    "count": len(stage_items),
                    "items": stage_items,
                    "progress": {"current": 0, "total": len(stage_items), "percent": 0},
                    "metadata": {"operation_id": operation_id},
                },
            )
            yield sse_event(
                "step",
                {
                    "step": "devices",
                    "status": "started",
                    "message": "Starting devices synchronization.",
                },
            )

            async def _run_devices_sync():
                try:
                    return await create_proxmox_devices(
                        netbox_session=netbox_session,
                        clusters_status=cluster_status,
                        node=None,
                        tag=tag,
                        websocket=devices_bridge,
                        use_websocket=True,
                        overwrite_device_role=overwrite_flags.overwrite_device_role,
                        overwrite_device_type=overwrite_flags.overwrite_device_type,
                        overwrite_device_tags=overwrite_flags.overwrite_device_tags,
                        overwrite_flags=overwrite_flags,
                    )
                finally:
                    await devices_bridge.close()

            _devices_start = time.monotonic()
            devices_task = asyncio.create_task(_run_devices_sync())
            async for frame in devices_bridge.iter_sse():
                yield frame
            sync_nodes = await devices_task

            yield sse_event(
                "step",
                {
                    "step": "devices",
                    "status": "completed",
                    "message": "Devices synchronization finished.",
                    "result": {"count": len(sync_nodes)},
                    "duration_seconds": round(time.monotonic() - _devices_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "storage",
                    "status": "started",
                    "message": "Starting storage synchronization.",
                },
            )

            async def _run_storage_sync():
                try:
                    return await create_storages(
                        netbox_session=netbox_session,
                        pxs=pxs,
                        tag=tag,
                        websocket=storage_bridge,
                        use_websocket=True,
                        fetch_concurrency=fetch_max_concurrency
                        if fetch_max_concurrency is not None
                        else 8,
                        overwrite_flags=overwrite_flags,
                    )
                finally:
                    await storage_bridge.close()

            _storage_start = time.monotonic()
            storage_task = asyncio.create_task(_run_storage_sync())
            async for frame in storage_bridge.iter_sse():
                yield frame
            sync_storage = await storage_task

            yield sse_event(
                "step",
                {
                    "step": "storage",
                    "status": "completed",
                    "message": "Storage synchronization finished.",
                    "result": {"count": len(sync_storage)},
                    "duration_seconds": round(time.monotonic() - _storage_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "started",
                    "message": "Starting virtual machines synchronization.",
                },
            )

            async def _run_vms_sync():
                try:
                    return await create_virtual_machines(
                        netbox_session=netbox_session,
                        pxs=pxs,
                        cluster_status=cluster_status,
                        cluster_resources=cluster_resources,
                        custom_fields=custom_fields,
                        tag=tag,
                        websocket=vm_bridge,
                        use_websocket=True,
                        sync_vm_network=False,
                        overwrite_vm_role=overwrite_flags.overwrite_vm_role,
                        overwrite_vm_type=overwrite_flags.overwrite_vm_type,
                        overwrite_vm_tags=overwrite_flags.overwrite_vm_tags,
                        overwrite_vm_description=overwrite_flags.overwrite_vm_description,
                        overwrite_vm_custom_fields=overwrite_flags.overwrite_vm_custom_fields,
                        overwrite_flags=overwrite_flags,
                    )
                finally:
                    await vm_bridge.close()

            _vms_start = time.monotonic()
            vms_task = asyncio.create_task(_run_vms_sync())
            async for frame in vm_bridge.iter_sse():
                yield frame
            sync_vms = await vms_task

            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "completed",
                    "message": "Virtual machines synchronization finished.",
                    "result": {"count": len(sync_vms)},
                    "duration_seconds": round(time.monotonic() - _vms_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "virtual-disks",
                    "status": "started",
                    "message": "Starting virtual disks synchronization.",
                },
            )

            async def _run_disks_sync():
                try:
                    return await create_virtual_disks(
                        netbox_session=netbox_session,
                        pxs=pxs,
                        cluster_status=cluster_status,
                        cluster_resources=cluster_resources,
                        tag=tag,
                        websocket=disks_bridge,
                        use_websocket=True,
                        use_css=False,
                    )
                finally:
                    await disks_bridge.close()

            _disks_start = time.monotonic()
            disks_task = asyncio.create_task(_run_disks_sync())
            async for frame in disks_bridge.iter_sse():
                yield frame
            sync_disks = await disks_task

            yield sse_event(
                "step",
                {
                    "step": "virtual-disks",
                    "status": "completed",
                    "message": "Virtual disks synchronization finished.",
                    "result": {"count": _result_count(sync_disks)},
                    "duration_seconds": round(time.monotonic() - _disks_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "task-history",
                    "status": "started",
                    "message": "Starting task history synchronization.",
                },
            )

            async def _run_task_history_sync():
                try:
                    return await sync_all_virtual_machine_task_histories(
                        netbox_session=netbox_session,
                        pxs=pxs,
                        cluster_status=cluster_status,
                        tag_refs=tag_refs,
                        websocket=task_history_bridge,
                        use_websocket=True,
                        fetch_max_concurrency=fetch_max_concurrency,
                    )
                finally:
                    await task_history_bridge.close()

            _task_history_start = time.monotonic()
            task_history_task = asyncio.create_task(_run_task_history_sync())
            async for frame in task_history_bridge.iter_sse():
                yield frame
            sync_task_history = await task_history_task

            yield sse_event(
                "step",
                {
                    "step": "task-history",
                    "status": "completed",
                    "message": "Task history synchronization finished.",
                    "result": {"count": _result_count(sync_task_history)},
                    "duration_seconds": round(time.monotonic() - _task_history_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "backups",
                    "status": "started",
                    "message": "Starting backup synchronization.",
                },
            )

            async def _run_backups_sync():
                try:
                    return await _create_all_virtual_machine_backups(
                        netbox_session=netbox_session,
                        pxs=pxs,
                        cluster_status=cluster_status,
                        tag=tag,
                        delete_nonexistent_backup=True,
                        fetch_max_concurrency=fetch_max_concurrency,
                        websocket=backups_bridge,
                        use_websocket=True,
                    )
                finally:
                    await backups_bridge.close()

            _backups_start = time.monotonic()
            backups_task = asyncio.create_task(_run_backups_sync())
            async for frame in backups_bridge.iter_sse():
                yield frame
            sync_backups = await backups_task

            yield sse_event(
                "step",
                {
                    "step": "backups",
                    "status": "completed",
                    "message": "Backup synchronization finished.",
                    "result": {"count": len(sync_backups)},
                    "duration_seconds": round(time.monotonic() - _backups_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "snapshots",
                    "status": "started",
                    "message": "Starting snapshot synchronization.",
                },
            )

            async def _run_snapshots_sync():
                try:
                    return await _create_all_virtual_machine_snapshots(
                        netbox_session=netbox_session,
                        pxs=pxs,
                        cluster_status=cluster_status,
                        cluster_resources=cluster_resources,
                        tag=tag,
                        fetch_max_concurrency=fetch_max_concurrency,
                        websocket=snapshots_bridge,
                        use_websocket=True,
                    )
                finally:
                    await snapshots_bridge.close()

            _snapshots_start = time.monotonic()
            snapshots_task = asyncio.create_task(_run_snapshots_sync())
            async for frame in snapshots_bridge.iter_sse():
                yield frame
            sync_snapshots = await snapshots_task

            yield sse_event(
                "step",
                {
                    "step": "snapshots",
                    "status": "completed",
                    "message": "Snapshot synchronization finished.",
                    "result": {"count": _result_count(sync_snapshots)},
                    "duration_seconds": round(time.monotonic() - _snapshots_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "node-interfaces",
                    "status": "started",
                    "message": "Starting node interfaces synchronization.",
                },
            )

            async def _run_node_interfaces_sync():
                try:
                    return await create_all_device_interfaces(
                        netbox_session=netbox_session,
                        tag=tag,
                        clusters_status=cluster_status,
                        websocket=node_interfaces_bridge,
                        use_websocket=True,
                    )
                finally:
                    await node_interfaces_bridge.close()

            _node_interfaces_start = time.monotonic()
            node_interfaces_task = asyncio.create_task(_run_node_interfaces_sync())
            async for frame in node_interfaces_bridge.iter_sse():
                yield frame
            sync_node_interfaces = await node_interfaces_task

            yield sse_event(
                "step",
                {
                    "step": "node-interfaces",
                    "status": "completed",
                    "message": "Node interfaces synchronization finished.",
                    "result": {"count": len(sync_node_interfaces)},
                    "duration_seconds": round(time.monotonic() - _node_interfaces_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "vm-interfaces",
                    "status": "started",
                    "message": "Starting VM interfaces synchronization.",
                },
            )

            async def _run_vm_interfaces_sync():
                try:
                    return await create_only_vm_interfaces(
                        netbox_session=netbox_session,
                        pxs=pxs,
                        cluster_status=cluster_status,
                        cluster_resources=cluster_resources,
                        custom_fields=custom_fields,
                        tag=tag,
                        websocket=vm_interfaces_bridge,
                        use_websocket=True,
                        overwrite_flags=overwrite_flags,
                    )
                finally:
                    await vm_interfaces_bridge.close()

            _vm_interfaces_start = time.monotonic()
            vm_interfaces_task = asyncio.create_task(_run_vm_interfaces_sync())
            async for frame in vm_interfaces_bridge.iter_sse():
                yield frame
            sync_vm_interfaces = await vm_interfaces_task

            yield sse_event(
                "step",
                {
                    "step": "vm-interfaces",
                    "status": "completed",
                    "message": "VM interfaces synchronization finished.",
                    "result": {"count": len(sync_vm_interfaces)},
                    "duration_seconds": round(time.monotonic() - _vm_interfaces_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "vm-ip-addresses",
                    "status": "started",
                    "message": "Starting VM IP address synchronization.",
                },
            )

            async def _run_vm_ip_addresses_sync():
                try:
                    return await create_only_vm_ip_addresses(
                        netbox_session=netbox_session,
                        pxs=pxs,
                        cluster_status=cluster_status,
                        cluster_resources=cluster_resources,
                        custom_fields=custom_fields,
                        tag=tag,
                        websocket=vm_ip_addresses_bridge,
                        use_websocket=True,
                        overwrite_flags=overwrite_flags,
                    )
                finally:
                    await vm_ip_addresses_bridge.close()

            _vm_ip_addresses_start = time.monotonic()
            vm_ip_addresses_task = asyncio.create_task(_run_vm_ip_addresses_sync())
            async for frame in vm_ip_addresses_bridge.iter_sse():
                yield frame
            sync_vm_ip_addresses = await vm_ip_addresses_task

            yield sse_event(
                "step",
                {
                    "step": "vm-ip-addresses",
                    "status": "completed",
                    "message": "VM IP address synchronization finished.",
                    "result": {"count": len(sync_vm_ip_addresses)},
                    "duration_seconds": round(time.monotonic() - _vm_ip_addresses_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "replications",
                    "status": "started",
                    "message": "Starting replications synchronization.",
                },
            )

            async def _run_replications_sync():
                try:
                    return await sync_all_replications(
                        netbox_session=netbox_session,
                        pxs=pxs,
                    )
                finally:
                    await replications_bridge.close()

            _replications_start = time.monotonic()
            replications_task = asyncio.create_task(_run_replications_sync())
            async for frame in replications_bridge.iter_sse():
                yield frame
            sync_replications = await replications_task

            yield sse_event(
                "step",
                {
                    "step": "replications",
                    "status": "completed",
                    "message": "Replications synchronization finished.",
                    "result": {
                        "created": sync_replications.get("created", 0),
                        "updated": sync_replications.get("updated", 0),
                    },
                    "duration_seconds": round(time.monotonic() - _replications_start, 3),
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "backup-routines",
                    "status": "started",
                    "message": "Starting backup routines synchronization.",
                },
            )

            async def _run_backup_routines_sync():
                try:
                    return await sync_all_backup_routines(
                        netbox_session=netbox_session,
                        pxs=pxs,
                        bridge=backup_routines_bridge,
                    )
                finally:
                    await backup_routines_bridge.close()

            _backup_routines_start = time.monotonic()
            backup_routines_task = asyncio.create_task(_run_backup_routines_sync())
            async for frame in backup_routines_bridge.iter_sse():
                yield frame
            sync_backup_routines = await backup_routines_task

            yield sse_event(
                "step",
                {
                    "step": "backup-routines",
                    "status": "completed",
                    "message": "Backup routines synchronization finished.",
                    "result": {
                        "created": sync_backup_routines.get("created", 0),
                        "updated": sync_backup_routines.get("updated", 0),
                    },
                    "duration_seconds": round(time.monotonic() - _backup_routines_start, 3),
                },
            )

            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Full update sync completed.",
                    "result": {
                        "devices": sync_nodes,
                        "storage": sync_storage,
                        "virtual_machines": sync_vms,
                        "virtual_disks": sync_disks,
                        "task_history": sync_task_history,
                        "backups": sync_backups,
                        "snapshots": sync_snapshots,
                        "node_interfaces": sync_node_interfaces,
                        "vm_interfaces": sync_vm_interfaces,
                        "vm_ip_addresses": sync_vm_ip_addresses,
                        "replications": sync_replications,
                        "backup_routines": sync_backup_routines,
                        "devices_count": len(sync_nodes),
                        "storage_count": len(sync_storage),
                        "virtual_machines_count": len(sync_vms),
                        "virtual_disks_count": _result_count(sync_disks),
                        "task_history_count": _result_count(sync_task_history),
                        "backups_count": len(sync_backups),
                        "snapshots_count": _result_count(sync_snapshots),
                        "node_interfaces_count": len(sync_node_interfaces),
                        "vm_interfaces_count": len(sync_vm_interfaces),
                        "vm_ip_addresses_count": len(sync_vm_ip_addresses),
                        "replications_count": sync_replications.get("created", 0)
                        + sync_replications.get("updated", 0),
                        "backup_routines_count": sync_backup_routines.get("created", 0)
                        + sync_backup_routines.get("updated", 0),
                    },
                },
            )
        except asyncio.CancelledError:
            yield sse_event(
                "error",
                {
                    "step": "full-update",
                    "status": "failed",
                    "error": "Server shutdown or request cancelled.",
                    "detail": "Server shutdown or request cancelled.",
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Full update sync cancelled.",
                    "errors": [{"detail": "Server shutdown or request cancelled."}],
                },
            )
        except ProxboxException as error:
            yield sse_event(
                "error_detail",
                {
                    "event": "error_detail",
                    "phase": "full-update",
                    "category": ErrorCategory.INTERNAL.value,
                    "message": "Full update synchronization failed",
                    "detail": error.detail or error.message,
                    "suggestion": "Review backend logs and retry the full update",
                },
            )
            yield sse_event(
                "error",
                {
                    "step": "full-update",
                    "status": "failed",
                    "error": error.message,
                    "detail": error.detail,
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": error.message,
                    "errors": [{"detail": error.detail or error.message}],
                },
            )
        except Exception as error:  # noqa: BLE001
            yield sse_event(
                "error_detail",
                {
                    "event": "error_detail",
                    "phase": "full-update",
                    "category": ErrorCategory.INTERNAL.value,
                    "message": "Unexpected error during full update",
                    "detail": str(error),
                    "suggestion": "Check backend logs for stack trace and retry",
                },
            )
            yield sse_event(
                "error",
                {
                    "step": "full-update",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Full update sync failed.",
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


def register_full_update_routes(app) -> None:
    """Mount full-update routes on the root application."""
    app.include_router(full_update_router)
