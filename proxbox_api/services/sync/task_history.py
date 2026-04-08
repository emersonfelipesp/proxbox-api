"""Virtual machine task history synchronization service."""

from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime, timezone

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import RestRecord, rest_bulk_reconcile_async, rest_list_async
from proxbox_api.proxmox_to_netbox.models import NetBoxTaskHistorySyncState
from proxbox_api.services.proxmox_helpers import (
    dump_models,
    get_node_task_status,
    get_node_tasks,
)
from proxbox_api.services.sync.vmid_helpers import extract_proxmox_vmid

_DEFAULT_FETCH_CONCURRENCY = max(1, int(os.getenv("PROXBOX_PROXMOX_FETCH_CONCURRENCY", "4")))
_DEFAULT_VM_SYNC_CONCURRENCY = max(1, int(os.getenv("PROXBOX_NETBOX_WRITE_CONCURRENCY", "4")))
_TASK_HISTORY_PATCHABLE_FIELDS = frozenset(
    {
        "end_time",
        "status",
        "task_state",
        "exitstatus",
        "tags",
        "custom_fields",
    }
)


def _normalize_text(value: object) -> str | None:
    """Normalize text value, handling None, dict, and string types."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("name") or value.get("slug") or value.get("id")
    text = str(value).strip()
    return text or None


def _humanize_task_type(task_type: str) -> str:
    """Convert Proxmox task type to human-readable action.

    Examples:
        vzstart -> Start
        vzshutdown -> Shutdown
        qmstart -> Start
    """
    # Map common task type prefixes and suffixes
    mapping = {
        "start": "Start",
        "shutdown": "Shutdown",
        "reboot": "Reboot",
        "stop": "Stop",
        "pause": "Pause",
        "resume": "Resume",
        "suspend": "Suspend",
        "delete": "Delete",
        "create": "Create",
        "clone": "Clone",
        "move": "Move",
        "backup": "Backup",
        "restore": "Restore",
        "snapshot": "Snapshot",
        "rollback": "Rollback",
        "migrate": "Migrate",
        "convert": "Convert",
    }

    # Try to find matching suffix in the task type
    task_lower = task_type.lower()
    for key, value in mapping.items():
        if key in task_lower:
            return value

    # Fall back to titlecase
    return task_type.capitalize()


def _format_task_description(vm_type: str, task_id: str | None, task_type: str) -> str:
    """Format task description in human-readable format.

    Format: {VM_TYPE} {TASK_ID} - {ACTION}
    Examples:
        CT 144 - Start
        QEMU 100 - Shutdown
    """
    # Map VM type to display name
    vm_type_map = {
        "lxc": "CT",
        "qemu": "QEMU",
        "ct": "CT",
    }
    vm_display = vm_type_map.get(vm_type.lower(), vm_type.upper())

    # Humanize the task type
    action = _humanize_task_type(task_type)

    # Build the description
    if task_id:
        return f"{vm_display} {task_id} - {action}"
    return f"{vm_display} - {action}"


def _find_cluster_session(pxs: list | None, cluster_status: list | None, cluster_name: str | None):
    """Find Proxmox session for a cluster."""
    if not pxs or not cluster_status:
        return None

    for px, cs in zip(pxs, cluster_status or []):
        if cluster_name:
            cs_name = getattr(cs, "name", None)
            if cs_name == cluster_name:
                return px
        elif hasattr(cs, "node_list") and cs.node_list:
            return px
    return None


def _cluster_nodes(cluster_status: list | None, cluster_name: str | None) -> list[str]:
    """Get node names for a cluster."""
    if not cluster_status:
        return []

    for cs in cluster_status:
        cs_name = getattr(cs, "name", None)
        if cs_name == cluster_name or not cluster_name:
            nodes = getattr(cs, "node_list", None)
            if nodes:
                return [
                    getattr(n, "node", None) or getattr(n, "name", None)
                    for n in nodes
                    if hasattr(n, "node") or hasattr(n, "name")
                ]
    return []


def _build_task_payload(
    virtual_machine_id: int,
    vm_type: str,
    task: dict[str, object],
    task_status: dict[str, object],
    tag_refs: list[dict[str, object]],
    now: datetime,
) -> dict[str, object]:
    """Build task history payload from Proxmox task data."""
    start_time = task.get("starttime")
    if start_time:
        try:
            start_time_str = datetime.fromtimestamp(start_time, timezone.utc).isoformat()
        except (ValueError, OSError):
            start_time_str = now.isoformat()
    else:
        start_time_str = now.isoformat()

    end_time = task_status.get("endtime")
    end_time_str = None
    if end_time:
        try:
            end_time_str = datetime.fromtimestamp(end_time, timezone.utc).isoformat()
        except (ValueError, OSError):
            pass

    upid = _normalize_text(task.get("upid")) or ""
    task_id = _normalize_text(task.get("id"))
    task_type = _normalize_text(task.get("type")) or "unknown"
    node = _normalize_text(task.get("node")) or ""
    username = _normalize_text(task.get("user")) or "root@pam"

    description = _format_task_description(vm_type, task_id, task_type)

    # Use exitstatus if available, otherwise use status
    status = (
        _normalize_text(task_status.get("exitstatus"))
        or _normalize_text(task_status.get("status"))
        or "unknown"
    )
    task_state = _normalize_text(task_status.get("state"))
    exitstatus = _normalize_text(task_status.get("exitstatus"))

    pid_val = task.get("pid")
    if pid_val is not None:
        try:
            pid = int(pid_val)
        except (ValueError, TypeError):
            pid = None
    else:
        pid = None

    pstart_val = task.get("pstart")
    if pstart_val is not None:
        try:
            pstart = datetime.fromtimestamp(int(pstart_val), timezone.utc).isoformat()
        except (ValueError, TypeError):
            pstart = None
    else:
        pstart = None

    return {
        "virtual_machine": virtual_machine_id,
        "vm_type": vm_type,
        "upid": upid,
        "node": node,
        "pid": pid,
        "pstart": pstart,
        "task_id": task_id,
        "task_type": task_type,
        "username": username,
        "start_time": start_time_str,
        "end_time": end_time_str,
        "description": description,
        "status": status,
        "task_state": task_state,
        "exitstatus": exitstatus,
        "tags": tag_refs,
        "custom_fields": {},
    }


async def _list_all_vms_with_proxmox_id(
    nb,
    batch_size: int = 500,
) -> list[RestRecord]:
    """List all VMs from NetBox with pagination handling."""
    all_vms = []
    offset = 0

    while True:
        vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"limit": batch_size, "offset": offset},
        )
        if not vms:
            break
        all_vms.extend(vms)

        if len(vms) < batch_size:
            break
        offset += batch_size

    return all_vms


async def _sync_single_vm_task_history(
    vm: object,
    nb,
    pxs: list | None,
    cluster_status: list | None,
    normalized_tags: list[dict],
) -> tuple[int, int]:
    """Sync task history for a single VM. Returns (reconciled_count, skipped)."""
    reconciled = 0
    proxmox_vmid = extract_proxmox_vmid(vm)
    vm_name = vm.get("name", "unknown")
    vm_id = vm.get("id")

    if not proxmox_vmid:
        return (0, 1)

    proxmox_type = vm.get("type", "qemu")
    if proxmox_type not in ("qemu", "lxc"):
        proxmox_type = "qemu"

    cluster_name = None
    if vm.get("cluster"):
        cluster_name = (
            vm.get("cluster").get("name") if isinstance(vm.get("cluster"), dict) else None
        )

    if not cluster_name:
        for cs in cluster_status or []:
            if hasattr(cs, "node_list") and cs.node_list:
                cluster_name = getattr(cs, "name", None)
                break

    try:
        reconciled = await sync_virtual_machine_task_history(
            netbox_session=nb,
            pxs=pxs,
            cluster_status=cluster_status,
            virtual_machine_id=int(vm_id),
            vm_type=proxmox_type,
            cluster_name=cluster_name,
            tag_refs=normalized_tags,
        )
    except Exception as e:
        logger.warning(
            "Error syncing task history for VM %s (vmid=%s): %s",
            vm_name,
            proxmox_vmid,
            e,
        )
        return (0, 1)

    return (reconciled, 0)


async def sync_all_virtual_machine_task_histories(  # noqa: C901
    netbox_session: object,
    pxs: list[object] | None,
    cluster_status: list[object] | None,
    tag_refs: list[dict[str, object]] | None = None,
    websocket: object | None = None,
    use_websocket: bool = False,
    fetch_max_concurrency: int | None = None,
) -> dict[str, object]:
    """Sync task history for all Virtual Machines in NetBox."""

    nb = netbox_session
    if tag_refs is None:
        tag_refs = []
    normalized_tags = [tag for tag in tag_refs if tag.get("name") and tag.get("slug")]

    try:
        vms = await _list_all_vms_with_proxmox_id(nb)
    except Exception as e:
        logger.error(f"Error fetching VMs from NetBox for task history sync: {e}")
        return {"count": 0, "created": 0, "skipped": 0, "error": str(e)}

    vms_with_proxmox_id = [vm for vm in vms if extract_proxmox_vmid(vm)]
    if not vms_with_proxmox_id:
        logger.info("No VMs found with cf_proxmox_vm_id for task history sync")
        return {"count": 0, "created": 0, "skipped": 0}

    total_vms = len(vms_with_proxmox_id)
    total_reconciled = 0
    skipped = 0

    if use_websocket and websocket:
        await websocket.send_json(
            {
                "object": "task_history",
                "type": "sync",
                "data": {
                    "status": "started",
                    "message": f"Starting task history sync for {total_vms} VMs",
                },
            }
        )

    vm_sync_semaphore = asyncio.Semaphore(_DEFAULT_VM_SYNC_CONCURRENCY)

    async def _sync_vm_with_semaphore(vm):
        async with vm_sync_semaphore:
            return await _sync_single_vm_task_history(
                vm=vm,
                nb=nb,
                pxs=pxs,
                cluster_status=cluster_status,
                normalized_tags=normalized_tags,
            )

    sync_tasks = [_sync_vm_with_semaphore(vm) for vm in vms_with_proxmox_id]
    results = await asyncio.gather(*sync_tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            logger.warning("Task history sync task failed: %s", result)
            skipped += 1
        else:
            reconciled_count, skipped_count = result
            total_reconciled += reconciled_count
            skipped += skipped_count

    if use_websocket and websocket:
        await websocket.send_json({"object": "task_history", "end": True})

    logger.info(
        "Task history sync completed: %s records reconciled, %s skipped",
        total_reconciled,
        skipped,
    )
    return {
        "count": total_vms,
        "created": total_reconciled,
        "skipped": skipped,
    }


async def sync_virtual_machine_task_history(  # noqa: C901
    *,
    netbox_session: object,
    pxs: list[object] | None,
    cluster_status: list[object] | None,
    virtual_machine_id: int,
    vm_type: str,
    cluster_name: str | None,
    tag_refs: list[dict[str, object]] | None = None,
    websocket: object | None = None,
    use_websocket: bool = False,
    fetch_max_concurrency: int | None = None,
) -> int:
    """Sync archived Proxmox tasks into NetBox task history records for one VM."""

    nb = netbox_session
    proxmox_session = _find_cluster_session(pxs, cluster_status, cluster_name)
    if proxmox_session is None:
        logger.warning(
            "No Proxmox session found for cluster %s while syncing task history",
            cluster_name,
        )
        return 0

    node_names = _cluster_nodes(cluster_status, cluster_name)
    if not node_names:
        logger.warning(
            "No cluster nodes found for cluster %s while syncing task history",
            cluster_name,
        )
        return 0

    normalized_tags = [tag for tag in (tag_refs or []) if tag.get("name") and tag.get("slug")]
    fetch_semaphore = asyncio.Semaphore(fetch_max_concurrency or _DEFAULT_FETCH_CONCURRENCY)
    seen_upids: set[str] = set()
    now = datetime.now(timezone.utc)
    task_payloads: list[dict[str, object]] = []

    for node_name in node_names:
        try:
            async with fetch_semaphore:
                raw_tasks = get_node_tasks(
                    proxmox_session,
                    node=node_name,
                    vmid=virtual_machine_id,
                )
                if inspect.isawaitable(raw_tasks):
                    raw_tasks = await raw_tasks
                tasks = dump_models(raw_tasks)
        except Exception as error:
            logger.warning(
                "Error fetching task history for VM %s on node %s: %s",
                virtual_machine_id,
                node_name,
                error,
            )
            continue

        for task in tasks:
            upid = _normalize_text(task.get("upid"))
            if not upid or upid in seen_upids:
                continue
            seen_upids.add(upid)

            try:
                async with fetch_semaphore:
                    task_status = get_node_task_status(proxmox_session, node=node_name, upid=upid)
                    if inspect.isawaitable(task_status):
                        task_status = await task_status
                status_payload = task_status.model_dump(
                    mode="python",
                    by_alias=True,
                    exclude_none=True,
                )
            except Exception as error:
                logger.warning(
                    "Error fetching task status for VM %s on node %s upid %s: %s",
                    virtual_machine_id,
                    node_name,
                    upid,
                    error,
                )
                status_payload = {}

            payload = _build_task_payload(
                virtual_machine_id=virtual_machine_id,
                vm_type=vm_type,
                task=task,
                task_status=status_payload,
                tag_refs=normalized_tags,
                now=now,
            )

            task_payloads.append(payload)

    if not task_payloads:
        return 0

    # Perform bulk reconciliation with a single API call
    try:
        reconcile_result = await rest_bulk_reconcile_async(
            nb,
            "/api/plugins/proxbox/task-history/",
            payloads=task_payloads,
            lookup_fields=["upid"],
            schema=NetBoxTaskHistorySyncState,
            current_normalizer=lambda record: {
                "virtual_machine": record.get("virtual_machine"),
                "vm_type": record.get("vm_type"),
                "upid": record.get("upid"),
                "node": record.get("node"),
                "pid": record.get("pid"),
                "pstart": record.get("pstart"),
                "task_id": record.get("task_id"),
                "task_type": record.get("task_type"),
                "username": record.get("username"),
                "start_time": record.get("start_time"),
                "end_time": record.get("end_time"),
                "description": record.get("description"),
                "status": record.get("status"),
                "task_state": record.get("task_state"),
                "exitstatus": record.get("exitstatus"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
            },
            patchable_fields=_TASK_HISTORY_PATCHABLE_FIELDS,
        )

        reconciled = reconcile_result.created + reconcile_result.updated
        logger.debug(
            "Task history bulk reconcile for VM %s: created=%s, updated=%s, unchanged=%s",
            virtual_machine_id,
            reconcile_result.created,
            reconcile_result.updated,
            reconcile_result.unchanged,
        )
        return reconciled

    except Exception as error:
        logger.error(
            "Error during bulk task history reconciliation for VM %s: %s",
            virtual_machine_id,
            error,
        )
        # Fall back to per-task writes on bulk failure
        reconciled = 0
        from proxbox_api.netbox_rest import rest_reconcile_async
        for payload in task_payloads:
            try:
                await rest_reconcile_async(
                    nb,
                    "/api/plugins/proxbox/task-history/",
                    lookup={"upid": payload.get("upid")},
                    payload=payload,
                    schema=NetBoxTaskHistorySyncState,
                    current_normalizer=lambda record: {
                        "virtual_machine": record.get("virtual_machine"),
                        "vm_type": record.get("vm_type"),
                        "upid": record.get("upid"),
                        "node": record.get("node"),
                        "pid": record.get("pid"),
                        "pstart": record.get("pstart"),
                        "task_id": record.get("task_id"),
                        "task_type": record.get("task_type"),
                        "username": record.get("username"),
                        "start_time": record.get("start_time"),
                        "end_time": record.get("end_time"),
                        "description": record.get("description"),
                        "status": record.get("status"),
                        "task_state": record.get("task_state"),
                        "exitstatus": record.get("exitstatus"),
                        "tags": record.get("tags"),
                        "custom_fields": record.get("custom_fields"),
                    },
                    patchable_fields=_TASK_HISTORY_PATCHABLE_FIELDS,
                )
                reconciled += 1
            except Exception as item_error:
                logger.warning(
                    "Error reconciling task history upid=%s for VM %s: %s",
                    payload.get("upid"),
                    virtual_machine_id,
                    item_error,
                )

        return reconciled
