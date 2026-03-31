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
