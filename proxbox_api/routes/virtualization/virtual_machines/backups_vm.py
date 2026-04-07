"""VM backup discovery, batch processing, and sync routes."""

import asyncio
import os
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    RestRecord,
    rest_bulk_create_async,
    rest_bulk_delete_async,
    rest_bulk_patch_async,
    rest_create_async,
    rest_list_async,
    rest_list_paginated_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.proxmox_to_netbox.models import NetBoxBackupSyncState
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep
from proxbox_api.services.proxmox_helpers import dump_models, get_node_storage_content
from proxbox_api.services.sync.storage_links import (
    build_storage_index,
    storage_name_from_volume_id,
)
from proxbox_api.services.sync.vm_helpers import parse_comma_separated_ints
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

router = APIRouter()
_DEFAULT_FETCH_CONCURRENCY = max(1, int(os.getenv("PROXBOX_PROXMOX_FETCH_CONCURRENCY", "8")))
_DEFAULT_BACKUP_BATCH_SIZE = max(1, int(os.getenv("PROXBOX_BACKUP_BATCH_SIZE", "5")))
_DEFAULT_BACKUP_BATCH_DELAY_MS = max(0, int(os.getenv("PROXBOX_BACKUP_BATCH_DELAY_MS", "200")))
_DEFAULT_BULK_BATCH_SIZE = max(1, int(os.getenv("PROXBOX_BULK_BATCH_SIZE", "50")))
_DEFAULT_BULK_BATCH_DELAY_MS = max(0, int(os.getenv("PROXBOX_BULK_BATCH_DELAY_MS", "500")))

_BACKUP_SUBTYPE_ALIASES: dict[str, str] = {
    "ct": "lxc",
    "lxc": "lxc",
    "qemu": "qemu",
    "vm": "qemu",
}

_BACKUP_FORMAT_ALIASES: dict[str, str] = {
    "zst": "tzst",
    "vma.zst": "tzst",
    "vma.zstd": "tzst",
    "pbs-ct": "pbs-ct",
    "pbs-vm": "pbs-vm",
    "qcow2": "qcow2",
    "raw": "raw",
    "tgz": "tgz",
    "tbz": "tbz",
    "tar": "tar",
    "iso": "iso",
    "tzst": "tzst",
}


def _normalize_backup_subtype(raw_subtype: object, volume_id: object) -> str:
    text = str(raw_subtype or "").strip().lower()
    if text in _BACKUP_SUBTYPE_ALIASES:
        return _BACKUP_SUBTYPE_ALIASES[text]

    volid = str(volume_id or "").lower()
    if "/ct/" in volid:
        return "lxc"
    if "/vm/" in volid:
        return "qemu"
    return "undefined"


def _normalize_backup_format(raw_format: object, volume_id: object) -> str:
    text = str(raw_format or "").strip().lower()
    if text in _BACKUP_FORMAT_ALIASES:
        return _BACKUP_FORMAT_ALIASES[text]

    volid = str(volume_id or "").lower()
    if "/ct/" in volid:
        return "pbs-ct"
    if "/vm/" in volid:
        return "pbs-vm"
    return "undefined"


def _volids_from_proxmox_storage_backup_items(items: list[dict]) -> set[str]:
    """Collect Proxmox volume IDs for backup content rows (volid / NetBox volume_id)."""
    out: set[str] = set()
    for item in items:
        if item.get("content") != "backup":
            continue
        vid = item.get("volid")
        if isinstance(vid, str) and vid:
            out.add(vid)
    return out


def _relation_id_or_none(value):
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _load_storage_index(netbox_session) -> dict[tuple[str, str], dict]:
    nb = netbox_session
    try:
        storage_records = await rest_list_async(nb, "/api/plugins/proxbox/storage/")
    except Exception as error:
        error_detail = getattr(error, "detail", str(error))
        error_msg = f"{type(error).__name__}: {error_detail}"
        logger.warning("Error loading storage records for backup sync: %s", error_msg)
        return {}
    return build_storage_index(storage_records)


def _build_backup_normalizer():
    return lambda record: {
        "storage": record.get("storage"),
        "virtual_machine": _relation_id_or_none(record.get("virtual_machine")),
        "subtype": record.get("subtype"),
        "creation_time": record.get("creation_time"),
        "size": record.get("size"),
        "verification_state": record.get("verification_state"),
        "verification_upid": record.get("verification_upid"),
        "volume_id": record.get("volume_id"),
        "notes": record.get("notes"),
        "vmid": record.get("vmid"),
        "format": record.get("format"),
    }


def compute_backup_payload(
    backup: dict,
    vm_cache: dict[int, dict | None] | None = None,
    storage_index: dict[tuple[str, str], dict] | None = None,
    cluster_name: str | None = None,
) -> dict | None:
    """Pure function: transform a Proxmox backup dict into a NetBox payload dict.

    Returns None if the backup cannot be processed (no vmid, no VM found).
    Does NOT perform any HTTP requests.
    """
    if not isinstance(backup, dict):
        return None

    vmid = backup.get("vmid", None)
    if not vmid:
        return None

    vmid_int = int(vmid)
    virtual_machine = vm_cache.get(vmid_int) if vm_cache is not None else None
    if virtual_machine is None:
        return None

    if not virtual_machine:
        return None

    verification = backup.get("verification", {})
    verification_state = verification.get("state")
    verification_upid = verification.get("upid")

    volume_id = backup.get("volid", None)
    storage_name = storage_name_from_volume_id(volume_id)

    creation_time = None
    ctime = backup.get("ctime", None)
    if ctime:
        creation_time = datetime.fromtimestamp(ctime).isoformat()

    return {
        "storage": storage_name,
        "virtual_machine": virtual_machine.get("id"),
        "subtype": _normalize_backup_subtype(backup.get("subtype"), volume_id),
        "creation_time": creation_time,
        "size": backup.get("size"),
        "verification_state": verification_state,
        "verification_upid": verification_upid,
        "volume_id": volume_id,
        "notes": backup.get("notes"),
        "vmid": vmid,
        "format": _normalize_backup_format(backup.get("format"), volume_id),
    }


async def create_netbox_backups(
    backup,
    netbox_session: NetBoxSessionDep,
    *,
    cluster_name: str | None = None,
    storage_index: dict[tuple[str, str], dict] | None = None,
    vm_cache: dict[int, dict | None] | None = None,
):
    nb = netbox_session
    vmid_log: str | int | None = None
    try:
        if not isinstance(backup, dict):
            return None
        # Get the virtual machine on NetBox by the VM ID.
        vmid = backup.get("vmid", None)
        vmid_log = vmid
        if not vmid:
            return None

        vmid_int = int(vmid)
        virtual_machine = vm_cache.get(vmid_int) if vm_cache is not None else None
        if virtual_machine is None:
            # Get the virtual machine on NetBox by the VM ID using custom field filter
            vms = await rest_list_async(
                nb,
                "/api/virtualization/virtual-machines/",
                query={"cf_proxmox_vm_id": vmid_int},
            )
            virtual_machine = vms[0] if vms else None
            if vm_cache is not None:
                vm_cache[vmid_int] = virtual_machine

        if not virtual_machine:
            return None

        # Process verification data
        verification = backup.get("verification", {})
        verification_state = verification.get("state")
        verification_upid = verification.get("upid")

        # Process storage and volume data
        volume_id = backup.get("volid", None)
        storage_name = storage_name_from_volume_id(volume_id)

        creation_time = None
        ctime = backup.get("ctime", None)
        if ctime:
            creation_time = datetime.fromtimestamp(ctime).isoformat()

        backup_payload = {
            "storage": storage_name,
            "virtual_machine": virtual_machine.get("id"),
            "subtype": _normalize_backup_subtype(backup.get("subtype"), volume_id),
            "creation_time": creation_time,
            "size": backup.get("size"),
            "verification_state": verification_state,
            "verification_upid": verification_upid,
            "volume_id": volume_id,
            "notes": backup.get("notes"),
            "vmid": vmid,
            "format": _normalize_backup_format(backup.get("format"), volume_id),
        }

        netbox_backup = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/backups/",
            lookup={"volume_id": volume_id},
            payload=backup_payload,
            schema=NetBoxBackupSyncState,
            current_normalizer=lambda record: {
                "storage": record.get("storage"),
                "virtual_machine": _relation_id_or_none(record.get("virtual_machine")),
                "subtype": record.get("subtype"),
                "creation_time": record.get("creation_time"),
                "size": record.get("size"),
                "verification_state": record.get("verification_state"),
                "verification_upid": record.get("verification_upid"),
                "volume_id": record.get("volume_id"),
                "notes": record.get("notes"),
                "vmid": record.get("vmid"),
                "format": record.get("format"),
            },
        )

        # Create a journal entry for the backup
        await rest_create_async(
            nb,
            "/api/extras/journal-entries/",
            {
                "assigned_object_type": "netbox_proxbox.vmbackup",
                "assigned_object_id": netbox_backup.id,
                "kind": "info",
                "comments": f"Backup created for VM {vmid} in storage {storage_name}",
            },
        )

        return netbox_backup

    except Exception as error:
        error_detail = getattr(error, "detail", str(error))
        error_msg = f"{type(error).__name__}: {error_detail}"
        logger.warning("Error creating NetBox backup for VM %s: %s", vmid_log, error_msg)
        return None


def _normalize_existing_backup(
    record: RestRecord | dict,
) -> dict[str, object]:
    raw = record.serialize() if isinstance(record, RestRecord) else record
    return {
        "storage": raw.get("storage"),
        "virtual_machine": _relation_id_or_none(raw.get("virtual_machine")),
        "subtype": raw.get("subtype"),
        "creation_time": raw.get("creation_time"),
        "size": raw.get("size"),
        "verification_state": raw.get("verification_state"),
        "verification_upid": raw.get("verification_upid"),
        "volume_id": raw.get("volume_id"),
        "notes": raw.get("notes"),
        "vmid": raw.get("vmid"),
        "format": raw.get("format"),
    }


def _compute_backup_diff(
    desired: dict[str, object],
    current: dict[str, object],
) -> dict[str, object]:
    diff: dict[str, object] = {}
    for key, value in desired.items():
        if current.get(key) != value:
            diff[key] = value
    return diff


async def _bulk_reconcile_backups(  # noqa: C901
    nb,
    proxmox_backup_payloads: list[dict],
    proxmox_volume_ids: set[str],
    bulk_batch_size: int | None = None,
    bulk_batch_delay_ms: int | None = None,
) -> tuple[list[dict], int, int]:
    """Two-pass bulk reconcile: pre-fetch all existing, diff in memory, dispatch bulk ops.

    Returns (results, create_count, patch_count).
    """
    batch_size = bulk_batch_size or _DEFAULT_BULK_BATCH_SIZE
    delay_ms = (
        bulk_batch_delay_ms if bulk_batch_delay_ms is not None else _DEFAULT_BULK_BATCH_DELAY_MS
    )
    normalizer = _build_backup_normalizer()

    existing_backups_raw = await rest_list_paginated_async(nb, "/api/plugins/proxbox/backups/")
    existing_by_volume_id: dict[str, RestRecord] = {}
    for rec in existing_backups_raw:
        vid = rec.get("volume_id")
        if vid:
            existing_by_volume_id[str(vid)] = rec

    to_create: list[dict] = []
    to_patch: list[tuple[RestRecord, dict]] = []
    results_payloads: list[dict] = []
    journal_entries: list[dict] = []

    for payload in proxmox_backup_payloads:
        volume_id = payload.get("volume_id")
        existing = existing_by_volume_id.get(str(volume_id)) if volume_id else None

        if existing is None:
            validated = NetBoxBackupSyncState.model_validate(payload)
            to_create.append(validated.model_dump(exclude_none=True, by_alias=True))
            continue

        current_normalized = _normalize_existing_backup(existing)
        desired_model = NetBoxBackupSyncState.model_validate(payload)
        desired_normalized = desired_model.model_dump(exclude_none=True, by_alias=True)
        diff = _compute_backup_diff(desired_normalized, current_normalized)
        if diff:
            to_patch.append((existing, diff))
        else:
            results_payloads.append(existing.serialize())

    created_count = 0
    for i in range(0, len(to_create), batch_size):
        batch = to_create[i : i + batch_size]
        try:
            created = await rest_bulk_create_async(nb, "/api/plugins/proxbox/backups/", batch)
            created_count += len(created)
            for rec in created:
                results_payloads.append(rec.serialize())
                journal_entries.append(
                    {
                        "assigned_object_type": "netbox_proxbox.vmbackup",
                        "assigned_object_id": rec.id,
                        "kind": "info",
                        "comments": f"Backup created for VM {rec.get('vmid')} in storage {rec.get('storage')}",
                    }
                )
        except Exception:
            logger.warning(
                "Bulk create batch failed (%s items), falling back to individual creates",
                len(batch),
                exc_info=True,
            )
            for single_payload in batch:
                try:
                    rec = await rest_reconcile_async(
                        nb,
                        "/api/plugins/proxbox/backups/",
                        lookup={"volume_id": single_payload.get("volume_id")},
                        payload=single_payload,
                        schema=NetBoxBackupSyncState,
                        current_normalizer=normalizer,
                    )
                    created_count += 1
                    results_payloads.append(rec.serialize())
                    journal_entries.append(
                        {
                            "assigned_object_type": "netbox_proxbox.vmbackup",
                            "assigned_object_id": rec.id,
                            "kind": "info",
                            "comments": f"Backup created for VM {rec.get('vmid')} in storage {rec.get('storage')}",
                        }
                    )
                except Exception:
                    logger.warning(
                        "Individual backup create failed for volume_id=%s",
                        single_payload.get("volume_id"),
                        exc_info=True,
                    )

        if i + batch_size < len(to_create) and delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    patched_count = 0
    for i in range(0, len(to_patch), batch_size):
        batch = to_patch[i : i + batch_size]
        bulk_updates = []
        for existing_rec, diff in batch:
            patch_payload = dict(diff)
            patch_payload["id"] = existing_rec.id
            bulk_updates.append(patch_payload)

        try:
            patched = await rest_bulk_patch_async(nb, "/api/plugins/proxbox/backups/", bulk_updates)
            patched_count += len(patched)
            for rec in patched:
                results_payloads.append(rec.serialize())
                journal_entries.append(
                    {
                        "assigned_object_type": "netbox_proxbox.vmbackup",
                        "assigned_object_id": rec.id,
                        "kind": "info",
                        "comments": f"Backup updated for VM {rec.get('vmid')} in storage {rec.get('storage')}",
                    }
                )
        except Exception:
            logger.warning(
                "Bulk patch batch failed (%s items), falling back to individual patches",
                len(batch),
                exc_info=True,
            )
            for existing_rec, diff in batch:
                try:
                    for field, value in diff.items():
                        setattr(existing_rec, field, value)
                    await existing_rec.save()
                    patched_count += 1
                    results_payloads.append(existing_rec.serialize())
                except Exception:
                    logger.warning(
                        "Individual backup patch failed for id=%s",
                        existing_rec.id,
                        exc_info=True,
                    )

        if i + batch_size < len(to_patch) and delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    for i in range(0, len(journal_entries), batch_size):
        batch = journal_entries[i : i + batch_size]
        try:
            await rest_bulk_create_async(nb, "/api/extras/journal-entries/", batch)
        except Exception:
            logger.warning(
                "Bulk journal create failed (%s items), falling back to individual creates",
                len(batch),
                exc_info=True,
            )
            for entry in batch:
                try:
                    await rest_create_async(nb, "/api/extras/journal-entries/", entry)
                except Exception:
                    logger.debug("Individual journal create failed", exc_info=True)

        if i + batch_size < len(journal_entries) and delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    return results_payloads, created_count, patched_count


async def process_backups_batch(
    backup_tasks: list,
    batch_size: int = 10,
    delay_ms: int = 200,
) -> tuple[list, int]:
    """
    Process a list of backup tasks in batches to avoid overwhelming the API.

    Args:
        backup_tasks: List of async coroutine tasks to execute
        batch_size: Number of tasks to execute concurrently per batch (default: 10)
        delay_ms: Milliseconds to wait between batches (default: 200ms)

    Returns:
        (successful_reconcile_results, failure_count) where failures are exceptions from gather.
    """
    results: list = []
    failures = 0
    total_batches = (len(backup_tasks) + batch_size - 1) // batch_size

    for batch_idx, i in enumerate(range(0, len(backup_tasks), batch_size), start=1):
        batch = backup_tasks[i : i + batch_size]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)

        for r in batch_results:
            if isinstance(r, Exception):
                failures += 1
            elif r is not None:
                results.append(r)

        # Log progress every 10 batches to avoid log spam
        if batch_idx % 10 == 0 or batch_idx == total_batches:
            logger.info(
                "Backup sync progress: batch %d/%d (%d items processed, %d failures)",
                batch_idx,
                total_batches,
                len(results),
                failures,
            )

        # Delay between batches to allow NetBox DB connections to release
        if i + batch_size < len(backup_tasks) and delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    return results, failures


async def get_node_backups(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    node: str,
    storage: str,
    netbox_session: NetBoxSessionDep,
    storage_index: dict[tuple[str, str], dict] | None = None,
    vmid: str | None = None,
) -> tuple[list[dict], set[str]]:
    """Get raw backup dicts for a specific node and storage.

    Returns:
        (list of Proxmox backup dicts, set of Proxmox volid strings seen on storage)
    """
    for proxmox, cluster in zip(pxs, cluster_status):
        if cluster and cluster.node_list:
            for cluster_node in cluster.node_list:
                if cluster_node.name == node:
                    try:
                        raw_backups = await get_node_storage_content(
                            proxmox,
                            node=node,
                            storage=storage,
                            vmid=vmid,
                            content="backup",
                        )
                        backups = dump_models(raw_backups)

                        if vmid is not None:
                            filtered_vmid = str(vmid).strip()
                            backups = [
                                backup
                                for backup in backups
                                if str(backup.get("vmid", "")).strip() == filtered_vmid
                            ]

                        volids = _volids_from_proxmox_storage_backup_items(backups)
                        filtered = [b for b in backups if b.get("content") == "backup"]
                        return filtered, volids
                    except Exception as error:
                        logger.warning("Error getting backups for node %s: %s", node, error)
                        continue
    return [], set()


@router.get("/backups/create")
async def create_virtual_machine_backups(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    node: Annotated[
        str,
        Query(
            title="Node",
            description="The name of the node to retrieve the storage content for.",
        ),
    ],
    storage: Annotated[
        str,
        Query(
            title="Storage",
            description="The name of the storage to retrieve the content for.",
        ),
    ],
    vmid: Annotated[
        str | None,
        Query(title="VM ID", description="The ID of the VM to retrieve the content for."),
    ] = None,
):
    nb = netbox_session
    storage_index = await _load_storage_index(nb)
    raw_backups, volids = await get_node_backups(
        pxs,
        cluster_status,
        node,
        storage,
        netbox_session=netbox_session,
        storage_index=storage_index,
        vmid=vmid,
    )
    if not raw_backups:
        raise ProxboxException(message="Node or Storage not found.")

    vm_cache = await _prefetch_vm_cache(nb)
    payloads: list[dict] = []
    for backup in raw_backups:
        payload = compute_backup_payload(
            backup,
            vm_cache=vm_cache,
            storage_index=storage_index,
        )
        if payload is not None:
            payloads.append(payload)

    if not payloads:
        raise ProxboxException(message="No valid backups to process.")

    results, _created, _patched = await _bulk_reconcile_backups(nb, payloads, volids)
    return results


async def _prefetch_vm_cache(nb) -> dict[int, dict | None]:
    """Load all VMs from NetBox into a cache keyed by cf_proxmox_vm_id."""
    vms = await rest_list_async(nb, "/api/virtualization/virtual-machines/")
    cache: dict[int, dict | None] = {}
    for vm in vms:
        cf = vm.get("custom_fields", {}) or {}
        raw_vmid = cf.get("proxmox_vm_id")
        if raw_vmid is not None:
            try:
                vmid_int = int(str(raw_vmid).strip())
                cache[vmid_int] = vm.serialize() if hasattr(vm, "serialize") else vm.dict()
            except (ValueError, TypeError):
                continue
    return cache


async def _create_all_virtual_machine_backups(  # noqa: C901
    netbox_session,
    pxs,
    cluster_status,
    tag,
    delete_nonexistent_backup=False,
    fetch_max_concurrency: int | None = None,
    websocket=None,
    use_websocket=False,
    vmid_filter: str | None = None,
):
    """Internal function that handles backup sync with optional websocket support.

    Uses a two-pass bulk reconcile pattern:
    1. Discovery: fetch Proxmox backups and build payloads (no NetBox writes)
    2. Reconcile: pre-fetch all existing NetBox backups, diff in memory, bulk dispatch

    When ``vmid_filter`` is provided only backups belonging to that Proxmox VMID
    are fetched and synced.
    """
    nb = netbox_session
    results = []
    failure_count = 0
    deleted_count = 0
    backup_sync_ok = False
    storage_index = await _load_storage_index(nb)

    try:
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "backups",
                    "status": "started",
                    "message": "Starting backup synchronization.",
                }
            )

        vm_cache = await _prefetch_vm_cache(nb)

        all_raw_backups: list[dict] = []
        proxmox_backups: set[str] = set()
        discovery_tasks: list[asyncio.Task] = []
        fetch_semaphore = asyncio.Semaphore(fetch_max_concurrency or _DEFAULT_FETCH_CONCURRENCY)

        async def _discover_backups_for_node_storage(
            proxmox,
            cluster_name: str,
            node_name: str,
            storage_name: str,
        ) -> tuple[list[dict], set[str]]:
            async with fetch_semaphore:
                _extra: dict = {}
                if vmid_filter is not None:
                    _extra["vmid"] = vmid_filter
                raw_backups = await get_node_storage_content(
                    proxmox,
                    node=node_name,
                    storage=storage_name,
                    content="backup",
                    **_extra,
                )
                backups = dump_models(raw_backups)
                if vmid_filter is not None:
                    filtered_vmid = str(vmid_filter).strip()
                    backups = [
                        backup
                        for backup in backups
                        if str(backup.get("vmid", "")).strip() == filtered_vmid
                    ]
                volids = _volids_from_proxmox_storage_backup_items(backups)
                filtered = [b for b in backups if b.get("content") == "backup"]
            return filtered, volids

        for proxmox, cluster in zip(pxs, cluster_status):
            cluster_name = getattr(cluster, "name", None) if cluster else None
            storage_payload = await resolve_async(proxmox.session.storage.get())
            storage_list = [
                {
                    "storage": storage_dict.get("storage"),
                    "nodes": storage_dict.get("nodes", "all"),
                }
                for storage_dict in storage_payload
                if "backup" in storage_dict.get("content")
            ]

            if cluster and cluster.node_list:
                for cluster_node in cluster.node_list:
                    for storage in storage_list:
                        if storage.get("nodes") == "all" or cluster_node.name in storage.get(
                            "nodes", []
                        ):
                            discovery_tasks.append(
                                asyncio.create_task(
                                    _discover_backups_for_node_storage(
                                        proxmox=proxmox,
                                        cluster_name=cluster_name,
                                        node_name=cluster_node.name,
                                        storage_name=storage.get("storage"),
                                    )
                                )
                            )

        if discovery_tasks:
            discovery_results = await asyncio.gather(*discovery_tasks, return_exceptions=True)
            for result in discovery_results:
                if isinstance(result, Exception):
                    logger.warning("Backup discovery failed: %s", result, exc_info=True)
                    continue
                node_backups, node_volids = result
                all_raw_backups.extend(node_backups)
                proxmox_backups.update(node_volids)

        all_payloads: list[dict] = []
        for backup in all_raw_backups:
            payload = compute_backup_payload(
                backup,
                vm_cache=vm_cache,
                storage_index=storage_index,
            )
            if payload is not None:
                all_payloads.append(payload)

        if not all_payloads:
            error_msg = "No backups found to process"
            if use_websocket and websocket:
                await websocket.send_json(
                    {
                        "step": "backups",
                        "status": "warning",
                        "message": error_msg,
                    }
                )
            raise ProxboxException(message=error_msg)

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "backups",
                    "status": "discovered",
                    "message": f"Found {len(all_payloads)} backups to reconcile.",
                    "count": len(all_payloads),
                }
            )

        logger.info(
            "Starting bulk backup reconcile: %s payloads, batch_size=%s, delay_ms=%s",
            len(all_payloads),
            _DEFAULT_BULK_BATCH_SIZE,
            _DEFAULT_BULK_BATCH_DELAY_MS,
        )

        results, created_count, patched_count = await _bulk_reconcile_backups(
            nb,
            all_payloads,
            proxmox_backups,
        )

        logger.info(
            "Bulk reconcile completed: %s created, %s patched, %s total results",
            created_count,
            patched_count,
            len(results),
        )

        if delete_nonexistent_backup:
            try:
                netbox_backups = await rest_list_paginated_async(
                    nb,
                    "/api/plugins/proxbox/backups/",
                )
                ids_to_delete: list[int] = []
                skipped_no_volid = 0

                for backup in netbox_backups:
                    vid = backup.volume_id
                    if not vid:
                        skipped_no_volid += 1
                        continue
                    if vid not in proxmox_backups:
                        backup_id = backup.id
                        if backup_id is not None:
                            ids_to_delete.append(int(backup_id))

                if ids_to_delete:
                    batch_size = _DEFAULT_BULK_BATCH_SIZE
                    for i in range(0, len(ids_to_delete), batch_size):
                        batch_ids = ids_to_delete[i : i + batch_size]
                        try:
                            deleted = await rest_bulk_delete_async(
                                nb,
                                "/api/plugins/proxbox/backups/",
                                batch_ids,
                            )
                            deleted_count += deleted
                        except Exception:
                            logger.warning(
                                "Bulk delete failed (%s items), falling back to individual deletes",
                                len(batch_ids),
                                exc_info=True,
                            )
                            for bid in batch_ids:
                                try:
                                    await rest_bulk_delete_async(
                                        nb,
                                        "/api/plugins/proxbox/backups/",
                                        [bid],
                                    )
                                    deleted_count += 1
                                except Exception:
                                    logger.warning(
                                        "Failed to delete backup id=%s",
                                        bid,
                                        exc_info=True,
                                    )

                        if i + batch_size < len(ids_to_delete) and _DEFAULT_BULK_BATCH_DELAY_MS > 0:
                            await asyncio.sleep(_DEFAULT_BULK_BATCH_DELAY_MS / 1000.0)

                if skipped_no_volid:
                    logger.info(
                        "Skipped %s NetBox backup(s) with empty volume_id",
                        skipped_no_volid,
                    )
            except Exception:
                logger.warning("Error during backup deletion pass", exc_info=True)

        backup_sync_ok = True

        if use_websocket and websocket and backup_sync_ok:
            await websocket.send_json(
                {
                    "step": "backups",
                    "status": "completed",
                    "message": (
                        f"Backup sync completed. {len(results)} reconciled, "
                        f"{failure_count} task error(s), {deleted_count} deleted."
                    ),
                    "result": {
                        "reconciled": len(results),
                        "failed_tasks": failure_count,
                        "deleted": deleted_count,
                    },
                }
            )

    except ProxboxException:
        raise
    except Exception as error:
        error_msg = f"Error during backup sync: {str(error)}"
        logger.error(error_msg, exc_info=True)
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "backups",
                    "status": "failed",
                    "message": error_msg,
                    "error": str(error),
                }
            )
        raise ProxboxException(message=error_msg)

    logger.info("Syncing backups finished")
    return results


@router.get("/backups/all/create")
async def create_all_virtual_machine_backups(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    delete_nonexistent_backup: Annotated[
        bool,
        Query(
            title="Delete Nonexistent Backup",
            description="If true, deletes backups that exist in NetBox but not in Proxmox.",
        ),
    ] = False,
    fetch_max_concurrency: Annotated[
        int | None,
        Query(
            title="Fetch max concurrency",
            description="Maximum parallel Proxmox fetch operations for backup discovery.",
            ge=1,
        ),
    ] = None,
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description="Comma-separated list of NetBox VM IDs to sync. When provided, only these VMs will be synced.",
    ),
):
    vmid_filter_list = None
    vm_ids = parse_comma_separated_ints(netbox_vm_ids)
    if vm_ids:
        vmid_filter_list = await _get_proxmox_vmids_from_netbox_vm_ids(netbox_session, vm_ids)

    return await _create_all_virtual_machine_backups(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        tag=tag,
        delete_nonexistent_backup=delete_nonexistent_backup,
        fetch_max_concurrency=fetch_max_concurrency,
        vmid_filter=vmid_filter_list,
    )


async def _get_proxmox_vmids_from_netbox_vm_ids(
    netbox_session, netbox_vm_ids: list[int]
) -> list[int]:
    """Get Proxmox VM IDs from NetBox VM IDs."""
    if not netbox_vm_ids:
        return []

    try:
        vms = await rest_list_async(
            netbox_session,
            "/api/virtualization/virtual-machines/",
            query={"id": ",".join(str(vid) for vid in netbox_vm_ids)},
        )
        proxmox_vmids: list[int] = []
        if vms and isinstance(vms, list):
            for vm in vms:
                if not isinstance(vm, dict):
                    continue
                cf = vm.get("custom_fields", {}) or {}
                raw_vmid = cf.get("proxmox_vm_id")
                if raw_vmid is not None and str(raw_vmid).strip().isdigit():
                    proxmox_vmids.append(int(str(raw_vmid).strip()))
        return proxmox_vmids
    except Exception:
        return []


@router.get("/backups/all/create/stream", response_model=None)
async def create_all_virtual_machine_backups_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    delete_nonexistent_backup: Annotated[
        bool,
        Query(
            title="Delete Nonexistent Backup",
            description="If true, deletes backups that exist in NetBox but not in Proxmox.",
        ),
    ] = False,
    fetch_max_concurrency: Annotated[
        int | None,
        Query(
            title="Fetch max concurrency",
            description="Maximum parallel Proxmox fetch operations for backup discovery.",
            ge=1,
        ),
    ] = None,
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description="Comma-separated list of NetBox VM IDs to sync. When provided, only these VMs will be synced.",
    ),
):
    vmid_filter_list = None
    vm_ids = parse_comma_separated_ints(netbox_vm_ids)
    if vm_ids:
        vmid_filter_list = await _get_proxmox_vmids_from_netbox_vm_ids(netbox_session, vm_ids)

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await _create_all_virtual_machine_backups(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    tag=tag,
                    delete_nonexistent_backup=delete_nonexistent_backup,
                    fetch_max_concurrency=fetch_max_concurrency,
                    vmid_filter=vmid_filter_list,
                    websocket=bridge,
                    use_websocket=True,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "backups",
                    "status": "started",
                    "message": "Starting backup synchronization.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame
            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "backups",
                    "status": "completed",
                    "message": "Backup synchronization finished.",
                    "result": {"count": len(result) if result else 0},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Backup sync completed.",
                    "result": {"count": len(result) if result else 0},
                },
            )
        except Exception as error:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            yield sse_event(
                "error",
                {
                    "step": "backups",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Backup sync failed.",
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


@router.get("/{netbox_vm_id}/backups/create/stream", response_model=None)
async def create_virtual_machine_backups_by_id_stream(
    netbox_vm_id: int,
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    delete_nonexistent_backup: Annotated[
        bool,
        Query(
            title="Delete Nonexistent Backup",
            description="If true, deletes backups that exist in NetBox but not in Proxmox.",
        ),
    ] = False,
    fetch_max_concurrency: Annotated[
        int | None,
        Query(
            title="Fetch max concurrency",
            description="Maximum parallel Proxmox fetch operations for backup discovery.",
            ge=1,
        ),
    ] = None,
):
    """Sync backups for a single NetBox VM identified by its primary key."""
    vm_record = await asyncio.to_thread(
        lambda: netbox_session.virtualization.virtual_machines.get(id=netbox_vm_id)
    )
    if vm_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Virtual machine id={netbox_vm_id} was not found in NetBox.",
        )

    vm_data = (
        vm_record
        if isinstance(vm_record, dict)
        else (vm_record.serialize() if hasattr(vm_record, "serialize") else dict(vm_record))
    )
    cf = vm_data.get("custom_fields") or {}
    raw_vmid = cf.get("proxmox_vm_id")
    proxmox_vmid: str | None = None
    if raw_vmid is not None:
        stripped = str(raw_vmid).strip()
        if stripped.isdigit():
            proxmox_vmid = stripped

    if proxmox_vmid is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Virtual machine id={netbox_vm_id} has no proxmox_vm_id custom field set; "
                "cannot filter backups."
            ),
        )

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await _create_all_virtual_machine_backups(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    tag=tag,
                    delete_nonexistent_backup=delete_nonexistent_backup,
                    fetch_max_concurrency=fetch_max_concurrency,
                    websocket=bridge,
                    use_websocket=True,
                    vmid_filter=proxmox_vmid,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "backups",
                    "status": "started",
                    "message": f"Starting backup sync for VM id={netbox_vm_id}.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame
            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "backups",
                    "status": "completed",
                    "message": "Backup synchronization finished.",
                    "result": {"count": len(result) if result else 0},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Backup sync completed.",
                    "result": {"count": len(result) if result else 0},
                },
            )
        except Exception as error:
            yield sse_event(
                "error",
                {
                    "step": "backups",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Backup sync failed.",
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
