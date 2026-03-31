"""Virtual machine task history synchronization service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxTaskHistorySyncState
from proxbox_api.services.proxmox_helpers import (
    dump_models,
    get_node_task_status,
    get_node_tasks,
)

_DEFAULT_FETCH_CONCURRENCY = 4


def _normalize_vmid(vmid):
    """Normalize VMID values for safe cross-system comparisons."""
    if vmid is None:
        return None
    vmid_str = str(vmid).strip()
    return vmid_str or None


def _extract_proxmox_vmid(vm: dict) -> str | None:
    """Extract Proxmox VMID from NetBox VM payload across known field layouts."""
    top_level_keys = (
        "cf_proxmox_vm_id",
        "proxmox_vm_id",
        "cf_proxmox_vmid",
        "proxmox_vmid",
    )
    for key in top_level_keys:
        normalized = _normalize_vmid(vm.get(key))
        if normalized:
            return normalized

    custom_fields = vm.get("custom_fields")
    if isinstance(custom_fields, dict):
        custom_field_keys = (
            "proxmox_vm_id",
            "cf_proxmox_vm_id",
            "proxmox_vmid",
            "cf_proxmox_vmid",
        )
        for key in custom_field_keys:
            normalized = _normalize_vmid(custom_fields.get(key))
            if normalized:
                return normalized
    return None


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _cluster_nodes(cluster_status, cluster_name: str | None) -> list[str]:
    nodes: list[str] = []
    for cluster in cluster_status or []:
        if getattr(cluster, "name", None) != cluster_name:
            continue
        for node in getattr(cluster, "node_list", None) or []:
            node_name = _normalize_text(getattr(node, "name", None))
            if node_name:
                nodes.append(node_name)
        break
    return nodes


def _find_cluster_session(pxs, cluster_status, cluster_name: str | None):
    for px, cluster in zip(pxs, cluster_status or []):
        if getattr(cluster, "name", None) == cluster_name:
            return px
    return None


def _build_task_payload(
    *,
    virtual_machine_id: int,
    vm_type: str,
    task: dict[str, Any],
    task_status: dict[str, Any],
    tag_refs: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    return {
        "virtual_machine": virtual_machine_id,
        "vm_type": vm_type,
        "upid": task_status.get("upid") or task.get("upid"),
        "node": task_status.get("node") or task.get("node"),
        "pid": task_status.get("pid") or task.get("pid"),
        "pstart": task_status.get("pstart") or task.get("pstart"),
        "task_id": task_status.get("id") or task.get("id"),
        "task_type": task_status.get("type") or task.get("type"),
        "username": task_status.get("user") or task.get("user"),
        "start_time": task_status.get("starttime") or task.get("starttime"),
        "end_time": task.get("endtime"),
        "status": task_status.get("exitstatus") or task_status.get("status") or task.get("status"),
        "task_state": task_status.get("status") or task.get("status"),
        "exitstatus": task_status.get("exitstatus"),
        "tags": tag_refs,
        "custom_fields": {"proxmox_last_updated": now.isoformat()},
    }


async def sync_all_virtual_machine_task_histories(
    netbox_session,
    pxs,
    cluster_status,
    tag_refs: list[dict[str, Any]] | None = None,
    websocket=None,
    use_websocket: bool = False,
    fetch_max_concurrency: int | None = None,
):
    """Sync task history for all Virtual Machines in NetBox."""
    from proxbox_api.netbox_rest import rest_list_async

    nb = netbox_session
    if tag_refs is None:
        tag_refs = []
    normalized_tags = [tag for tag in tag_refs if tag.get("name") and tag.get("slug")]

    try:
        vms = await rest_list_async(nb, "/api/virtualization/virtual-machines/")
    except Exception as e:
        logger.error(f"Error fetching VMs from NetBox for task history sync: {e}")
        return {"count": 0, "created": 0, "skipped": 0, "error": str(e)}

    vms_with_proxmox_id = [vm for vm in vms if _extract_proxmox_vmid(vm)]
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

    for vm in vms_with_proxmox_id:
        proxmox_vmid = _extract_proxmox_vmid(vm)
        vm_name = vm.get("name", "unknown")
        vm_id = vm.get("id")

        if not proxmox_vmid:
            skipped += 1
            continue

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
            total_reconciled += reconciled
        except Exception as e:
            logger.warning(
                "Error syncing task history for VM %s (vmid=%s): %s",
                vm_name,
                proxmox_vmid,
                e,
            )
            skipped += 1

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "object": "task_history",
                    "type": "sync",
                    "data": {
                        "name": vm_name,
                        "vmid": proxmox_vmid,
                        "reconciled": reconciled,
                    },
                }
            )

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


async def sync_virtual_machine_task_history(
    *,
    netbox_session,
    pxs,
    cluster_status,
    virtual_machine_id: int,
    vm_type: str,
    cluster_name: str | None,
    tag_refs: list[dict[str, Any]] | None = None,
    websocket=None,
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
    reconciled = 0
    now = datetime.now(timezone.utc)

    for node_name in node_names:
        try:
            async with fetch_semaphore:
                tasks = await asyncio.to_thread(
                    lambda: dump_models(
                        get_node_tasks(
                            proxmox_session,
                            node=node_name,
                            vmid=virtual_machine_id,
                        )
                    )
                )
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
                    task_status = await asyncio.to_thread(
                        lambda: get_node_task_status(proxmox_session, node=node_name, upid=upid)
                    )
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

            try:
                await rest_reconcile_async(
                    nb,
                    "/api/plugins/proxbox/task-history/",
                    lookup={"upid": upid},
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
                )
                reconciled += 1
            except Exception as error:
                logger.warning(
                    "Error reconciling task history for VM %s task %s: %s",
                    virtual_machine_id,
                    upid,
                    error,
                )

    return reconciled
