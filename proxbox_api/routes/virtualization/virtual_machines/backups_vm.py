"""VM backup discovery, batch processing, and sync routes."""

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    RestRecord,
    clear_rest_get_cache_for_path,
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
from proxbox_api.runtime_settings import get_int
from proxbox_api.services.proxmox_helpers import dump_models, get_node_storage_content
from proxbox_api.services.sync.storage_links import (
    build_storage_index,
    find_storage_record,
    storage_name_from_volume_id,
)
from proxbox_api.services.sync.vm_helpers import (
    list_netbox_virtual_machines_by_ids,
    parse_selected_netbox_vm_ids,
    relation_id,
    relation_name,
    to_mapping,
)
from proxbox_api.services.sync.vmid_helpers import (
    extract_proxmox_endpoint_id,
    extract_proxmox_session_endpoint_id,
    extract_proxmox_vmid,
    normalize_positive_int,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_stream_generator

router = APIRouter()


def _resolve_fetch_concurrency() -> int:
    return get_int(
        settings_key="proxmox_fetch_concurrency",
        env="PROXBOX_PROXMOX_FETCH_CONCURRENCY",
        default=8,
        minimum=1,
    )


def _resolve_backup_batch_size() -> int:
    return get_int(
        settings_key="backup_batch_size",
        env="PROXBOX_BACKUP_BATCH_SIZE",
        default=5,
        minimum=1,
    )


def _resolve_backup_batch_delay_ms() -> int:
    return get_int(
        settings_key="backup_batch_delay_ms",
        env="PROXBOX_BACKUP_BATCH_DELAY_MS",
        default=200,
        minimum=0,
    )


def _resolve_bulk_batch_size() -> int:
    return get_int(
        settings_key="bulk_batch_size",
        env="PROXBOX_BULK_BATCH_SIZE",
        default=50,
        minimum=1,
    )


def _resolve_bulk_batch_delay_ms() -> int:
    return get_int(
        settings_key="bulk_batch_delay_ms",
        env="PROXBOX_BULK_BATCH_DELAY_MS",
        default=500,
        minimum=0,
    )


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

_BACKUP_ENDPOINT_ID_KEY = "_proxbox_endpoint_id"
_BACKUP_CLUSTER_NAME_KEY = "_proxbox_cluster_name"
_MISSING = object()


def _normalize_cluster_name(value: object) -> str | None:
    name = str(value or "").strip()
    return name.casefold() if name else None


@dataclass(frozen=True, slots=True)
class _BackupVMScope:
    """Exact ownership of one explicitly selected NetBox VM."""

    netbox_vm_id: int
    endpoint_id: int
    cluster_name: str
    proxmox_vmid: int


@dataclass(slots=True)
class _BackupVMCache:
    """Resolve a backup to a VM without guessing across reused Proxmox VMIDs."""

    records: dict[tuple[int | None, str | None, int], dict[str, object] | None] = field(
        default_factory=dict
    )
    selected_scopes: tuple[_BackupVMScope, ...] = ()

    def _insert(
        self,
        key: tuple[int | None, str | None, int],
        record: dict[str, object],
    ) -> None:
        existing = self.records.get(key, _MISSING)
        if existing is _MISSING:
            self.records[key] = record
            return
        if existing is None:
            return
        if relation_id(existing.get("id")) != relation_id(record.get("id")):
            # A less-specific identity is unsafe once more than one VM owns it.
            self.records[key] = None

    def add(self, record: object) -> None:
        payload = to_mapping(record)
        vmid = normalize_positive_int(extract_proxmox_vmid(payload))
        if vmid is None:
            return
        endpoint_id = extract_proxmox_endpoint_id(payload)
        cluster_name = _normalize_cluster_name(relation_name(payload.get("cluster")))
        for key in (
            (endpoint_id, cluster_name, vmid),
            (endpoint_id, None, vmid),
            (None, cluster_name, vmid),
            (None, None, vmid),
        ):
            self._insert(key, payload)

    def resolve(
        self,
        *,
        endpoint_id: object,
        cluster_name: object,
        proxmox_vmid: object,
    ) -> dict[str, object] | None:
        vmid = normalize_positive_int(proxmox_vmid)
        if vmid is None:
            return None
        endpoint = normalize_positive_int(endpoint_id)
        cluster = _normalize_cluster_name(cluster_name)
        for key in (
            (endpoint, cluster, vmid),
            (endpoint, None, vmid),
            (None, cluster, vmid),
            (None, None, vmid),
        ):
            if key in self.records:
                candidate = self.records[key]
                if candidate is None:
                    return None
                candidate_endpoint = extract_proxmox_endpoint_id(candidate)
                candidate_cluster = _normalize_cluster_name(relation_name(candidate.get("cluster")))
                if (
                    endpoint is not None
                    and candidate_endpoint is not None
                    and candidate_endpoint != endpoint
                ):
                    continue
                if cluster is not None and candidate_cluster is not None:
                    if candidate_cluster != cluster:
                        continue
                return candidate
        return None

    def is_unambiguous_scope_owner(self, scope: _BackupVMScope) -> bool:
        """Return whether an exact Proxmox identity has this one NetBox owner."""

        candidate = self.records.get(
            (scope.endpoint_id, _normalize_cluster_name(scope.cluster_name), scope.proxmox_vmid)
        )
        return candidate is not None and relation_id(candidate.get("id")) == scope.netbox_vm_id


def _resolve_backup_virtual_machine(
    vm_cache: _BackupVMCache | dict[int, dict | None] | None,
    *,
    endpoint_id: object,
    cluster_name: object,
    proxmox_vmid: object,
) -> dict[str, object] | None:
    if isinstance(vm_cache, _BackupVMCache):
        return vm_cache.resolve(
            endpoint_id=endpoint_id,
            cluster_name=cluster_name,
            proxmox_vmid=proxmox_vmid,
        )
    vmid = normalize_positive_int(proxmox_vmid)
    if vmid is None or vm_cache is None:
        return None
    return vm_cache.get(vmid)


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
        "proxmox_storage": _relation_id_or_none(record.get("proxmox_storage")),
        "virtual_machine": _relation_id_or_none(record.get("virtual_machine")),
        "subtype": record.get("subtype"),
        "creation_time": record.get("creation_time"),
        "size": record.get("size"),
        "used": record.get("used"),
        "encrypted": record.get("encrypted"),
        "verification_state": record.get("verification_state"),
        "verification_upid": record.get("verification_upid"),
        "volume_id": record.get("volume_id"),
        "notes": record.get("notes"),
        "vmid": record.get("vmid"),
        "format": record.get("format"),
    }


def compute_backup_payload(
    backup: dict,
    vm_cache: _BackupVMCache | dict[int, dict | None] | None = None,
    storage_index: dict[tuple[str, str], dict] | None = None,
    cluster_name: str | None = None,
    endpoint_id: int | None = None,
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
    resolved_cluster_name = cluster_name or backup.get(_BACKUP_CLUSTER_NAME_KEY)
    resolved_endpoint_id = endpoint_id or backup.get(_BACKUP_ENDPOINT_ID_KEY)
    virtual_machine = _resolve_backup_virtual_machine(
        vm_cache,
        endpoint_id=resolved_endpoint_id,
        cluster_name=resolved_cluster_name,
        proxmox_vmid=vmid_int,
    )
    if virtual_machine is None:
        return None

    if not virtual_machine:
        return None

    verification = backup.get("verification", {})
    verification_state = verification.get("state")
    verification_upid = verification.get("upid")

    volume_id = backup.get("volid", None)
    storage_name = storage_name_from_volume_id(volume_id)

    proxmox_storage_id = None
    if storage_index and storage_name:
        storage_record = find_storage_record(
            storage_index,
            cluster_name=str(resolved_cluster_name or "") or None,
            storage_name=storage_name,
        )
        if storage_record:
            proxmox_storage_id = storage_record.get("id")

    creation_time = None
    ctime = backup.get("ctime", None)
    if ctime:
        creation_time = datetime.fromtimestamp(ctime).isoformat()

    return {
        "storage": storage_name,
        "proxmox_storage": proxmox_storage_id,
        "virtual_machine": virtual_machine.get("id"),
        "subtype": _normalize_backup_subtype(backup.get("subtype"), volume_id),
        "creation_time": creation_time,
        "size": backup.get("size"),
        "used": backup.get("used"),
        "encrypted": backup.get("encrypted"),
        "verification_state": verification_state,
        "verification_upid": verification_upid,
        "volume_id": volume_id,
        "notes": backup.get("notes"),
        "vmid": vmid,
        "format": _normalize_backup_format(backup.get("format"), volume_id),
    }


async def create_netbox_backups(  # noqa: C901
    backup,
    netbox_session: NetBoxSessionDep,
    *,
    cluster_name: str | None = None,
    endpoint_id: int | None = None,
    storage_index: dict[tuple[str, str], dict] | None = None,
    vm_cache: _BackupVMCache | dict[int, dict | None] | None = None,
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
        resolved_cluster_name = cluster_name or backup.get(_BACKUP_CLUSTER_NAME_KEY)
        resolved_endpoint_id = endpoint_id or backup.get(_BACKUP_ENDPOINT_ID_KEY)
        virtual_machine = _resolve_backup_virtual_machine(
            vm_cache,
            endpoint_id=resolved_endpoint_id,
            cluster_name=resolved_cluster_name,
            proxmox_vmid=vmid_int,
        )
        if virtual_machine is None:
            # Get the virtual machine on NetBox by the VM ID using custom field filter
            vms = await rest_list_async(
                nb,
                "/api/virtualization/virtual-machines/",
                query={"cf_proxmox_vm_id": vmid_int},
            )
            lookup_cache = vm_cache if isinstance(vm_cache, _BackupVMCache) else _BackupVMCache()
            for vm in vms:
                lookup_cache.add(vm)
            virtual_machine = lookup_cache.resolve(
                endpoint_id=resolved_endpoint_id,
                cluster_name=resolved_cluster_name,
                proxmox_vmid=vmid_int,
            )
            if isinstance(vm_cache, dict):
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

        proxmox_storage_id = None
        if storage_index and storage_name and resolved_cluster_name:
            storage_rec = find_storage_record(
                storage_index,
                cluster_name=str(resolved_cluster_name),
                storage_name=storage_name,
            )
            if storage_rec:
                proxmox_storage_id = storage_rec.get("id")

        backup_payload = {
            "storage": storage_name,
            "proxmox_storage": proxmox_storage_id,
            "virtual_machine": virtual_machine.get("id"),
            "subtype": _normalize_backup_subtype(backup.get("subtype"), volume_id),
            "creation_time": creation_time,
            "size": backup.get("size"),
            "used": backup.get("used"),
            "encrypted": backup.get("encrypted"),
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
            lookup={
                "virtual_machine": virtual_machine.get("id"),
                "volume_id": volume_id,
            },
            payload=backup_payload,
            schema=NetBoxBackupSyncState,
            current_normalizer=lambda record: {
                "storage": record.get("storage"),
                "proxmox_storage": _relation_id_or_none(record.get("proxmox_storage")),
                "virtual_machine": _relation_id_or_none(record.get("virtual_machine")),
                "subtype": record.get("subtype"),
                "creation_time": record.get("creation_time"),
                "size": record.get("size"),
                "used": record.get("used"),
                "encrypted": record.get("encrypted"),
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
        "proxmox_storage": _relation_id_or_none(raw.get("proxmox_storage")),
        "virtual_machine": _relation_id_or_none(raw.get("virtual_machine")),
        "subtype": raw.get("subtype"),
        "creation_time": raw.get("creation_time"),
        "size": raw.get("size"),
        "used": raw.get("used"),
        "encrypted": raw.get("encrypted"),
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


def _backup_owner_key(record: dict[str, object]) -> tuple[int, str]:
    virtual_machine_id = _relation_id_or_none(record.get("virtual_machine"))
    volume_id = str(record.get("volume_id") or "").strip()
    if virtual_machine_id is None or not volume_id:
        raise ProxboxException(
            message="Unable to reconcile backup without stable ownership",
            detail=(
                "Every backup must contain a NetBox virtual_machine id and non-empty volume_id."
            ),
            http_status_code=502,
        )
    return virtual_machine_id, volume_id


async def _bulk_reconcile_backups(  # noqa: C901
    nb,
    proxmox_backup_payloads: list[dict],
    bulk_batch_size: int | None = None,
    bulk_batch_delay_ms: int | None = None,
) -> tuple[list[dict], int, int]:
    """Two-pass bulk reconcile: pre-fetch all existing, diff in memory, dispatch bulk ops.

    Returns (results, create_count, patch_count).
    """
    batch_size = bulk_batch_size or _resolve_bulk_batch_size()
    delay_ms = (
        bulk_batch_delay_ms if bulk_batch_delay_ms is not None else _resolve_bulk_batch_delay_ms()
    )
    normalizer = _build_backup_normalizer()

    # Deduplicate input payloads by owning VM plus volume_id. PBS storage content lists all
    # backups for every VM; when multiple VMs share a PBS storage they can each
    # contribute the same volume_id to the payload list, causing bulk-create to
    # fail with a duplicate constraint even though no race condition is present.
    # A bare volume_id is not globally unique: separate endpoints may legitimately
    # expose identical PBS volume names for different NetBox VMs.
    deduped_by_owner: dict[tuple[int, str], dict] = {}
    for _p in proxmox_backup_payloads:
        owner_key = _backup_owner_key(_p)
        prior = deduped_by_owner.get(owner_key)
        if prior is not None and prior != _p:
            raise ProxboxException(
                message="Conflicting backup payloads share the same owner identity",
                detail=f"Duplicate backup owner key: {owner_key}.",
                http_status_code=502,
            )
        deduped_by_owner.setdefault(owner_key, _p)
    proxmox_backup_payloads = list(deduped_by_owner.values())

    # Force a fresh fetch: discard any cached list so the pre-fetch reflects the
    # actual current NetBox state and avoids placing already-existing records in
    # `to_create`, which would otherwise cause spurious 400 "already exists" errors.
    clear_rest_get_cache_for_path(nb, "/api/plugins/proxbox/backups/")
    existing_backups_raw = await rest_list_paginated_async(nb, "/api/plugins/proxbox/backups/")
    existing_by_owner: dict[tuple[int, str], RestRecord] = {}
    for rec in existing_backups_raw:
        raw = rec.serialize() if isinstance(rec, RestRecord) else to_mapping(rec)
        owner_key = _backup_owner_key(raw)
        if owner_key in existing_by_owner:
            raise ProxboxException(
                message="Duplicate NetBox backups share the same owner identity",
                detail=f"Duplicate existing backup owner key: {owner_key}.",
                http_status_code=502,
            )
        existing_by_owner[owner_key] = rec

    to_create: list[dict] = []
    to_patch: list[tuple[RestRecord, dict]] = []
    results_payloads: list[dict] = []
    journal_entries: list[dict] = []

    for payload in proxmox_backup_payloads:
        owner_key = _backup_owner_key(payload)
        existing = existing_by_owner.get(owner_key)

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
                        lookup={
                            "virtual_machine": single_payload.get("virtual_machine"),
                            "volume_id": single_payload.get("volume_id"),
                        },
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
    batch_size: int | None = None,
    delay_ms: int | None = None,
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
    if batch_size is None:
        batch_size = _resolve_backup_batch_size()
    if delay_ms is None:
        delay_ms = _resolve_backup_batch_delay_ms()

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
    """Build backup sync tasks for a specific node and storage.

    Returns:
        (list of backup sync tasks, set of Proxmox volid strings seen on storage)
    """
    for proxmox, cluster in zip(pxs, cluster_status):
        if cluster and cluster.node_list:
            for cluster_node in cluster.node_list:
                if cluster_node.name == node:
                    try:
                        raw_backups = get_node_storage_content(
                            proxmox,
                            node=node,
                            storage=storage,
                            vmid=vmid,
                            content="backup",
                        )
                        if inspect.isawaitable(raw_backups):
                            raw_backups = await raw_backups
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
                        tasks = [
                            create_netbox_backups(
                                backup,
                                netbox_session=netbox_session,
                                cluster_name=getattr(cluster, "name", None),
                                endpoint_id=extract_proxmox_session_endpoint_id(proxmox),
                                storage_index=storage_index,
                            )
                            for backup in filtered
                        ]
                        return tasks, volids
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
    backup_tasks, volids = await get_node_backups(
        pxs,
        cluster_status,
        node,
        storage,
        netbox_session=netbox_session,
        storage_index=storage_index,
        vmid=vmid,
    )
    if not backup_tasks:
        raise ProxboxException(message="Node or Storage not found.")

    results = await asyncio.gather(*backup_tasks)
    normalized_results = []
    for result in results:
        if result is None:
            continue
        if hasattr(result, "serialize"):
            normalized_results.append(result.serialize())
        else:
            normalized_results.append(result)
    if not normalized_results:
        raise ProxboxException(message="No valid backups to process.")
    return normalized_results


def _backup_vm_scope(record: object) -> _BackupVMScope | None:
    payload = to_mapping(record)
    netbox_vm_id = relation_id(payload.get("id"))
    endpoint_id = extract_proxmox_endpoint_id(payload)
    cluster_name = _normalize_cluster_name(relation_name(payload.get("cluster")))
    proxmox_vmid = normalize_positive_int(extract_proxmox_vmid(payload))
    if netbox_vm_id is None or endpoint_id is None or cluster_name is None or proxmox_vmid is None:
        return None
    return _BackupVMScope(
        netbox_vm_id=netbox_vm_id,
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        proxmox_vmid=proxmox_vmid,
    )


async def _prefetch_vm_cache(
    nb,
    netbox_vm_ids: list[int] | None = None,
) -> _BackupVMCache:
    """Load a collision-safe VM identity cache, optionally for exact NetBox IDs."""

    if netbox_vm_ids is None:
        vms = await rest_list_async(nb, "/api/virtualization/virtual-machines/")
    else:
        vms = await list_netbox_virtual_machines_by_ids(nb, netbox_vm_ids)

    cache = _BackupVMCache()
    for vm in vms:
        cache.add(vm)

    scopes = tuple(scope for vm in vms if (scope := _backup_vm_scope(vm)) is not None)
    cache.selected_scopes = tuple(sorted(scopes, key=lambda scope: scope.netbox_vm_id))

    if netbox_vm_ids is None:
        return cache

    requested_ids = set(netbox_vm_ids)
    resolved_ids = {
        vm_id for vm in vms if (vm_id := relation_id(to_mapping(vm).get("id"))) is not None
    }
    missing_ids = requested_ids - resolved_ids
    if missing_ids:
        raise ProxboxException(
            message="Unable to resolve explicitly selected NetBox VMs",
            detail=f"NetBox did not return selected VM id(s): {sorted(missing_ids)}.",
            http_status_code=502,
        )

    scoped_ids = {scope.netbox_vm_id for scope in scopes}
    invalid_ids = requested_ids - scoped_ids
    if invalid_ids:
        raise ProxboxException(
            message="Unable to verify selected backup VM ownership",
            detail=(
                "Selected VM id(s) are missing a positive NetBox id, Proxmox endpoint id, "
                f"cluster, or Proxmox VMID: {sorted(invalid_ids)}."
            ),
            http_status_code=502,
        )
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
    vmid_filter: str | int | list[int] | None = None,
    netbox_vm_ids: list[int] | None = None,
):
    """Internal function that handles backup sync with optional websocket support.

    Uses a two-pass bulk reconcile pattern:
    1. Discovery: fetch Proxmox backups and build payloads (no NetBox writes)
    2. Reconcile: pre-fetch all existing NetBox backups, diff in memory, bulk dispatch

    ``netbox_vm_ids`` carries exact NetBox/endpoint/cluster ownership. The legacy
    ``vmid_filter`` is retained for direct Proxmox-VMID callers, but must not be
    used to implement a NetBox-ID selection because VMIDs are endpoint-local.
    """
    nb = netbox_session
    if vmid_filter is None:
        selected_vmids: set[str] | None = None
    elif isinstance(vmid_filter, list):
        selected_vmids = {str(value).strip() for value in vmid_filter if str(value).strip()}
    else:
        selected_vmids = {str(vmid_filter).strip()}
    selected_netbox_ids = set(netbox_vm_ids) if netbox_vm_ids is not None else None
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

        vm_cache = (
            await _prefetch_vm_cache(nb)
            if netbox_vm_ids is None
            else await _prefetch_vm_cache(nb, netbox_vm_ids)
        )

        ambiguous_exact_identities = {
            (scope.endpoint_id, scope.cluster_name, scope.proxmox_vmid)
            for scope in vm_cache.selected_scopes
            if not vm_cache.is_unambiguous_scope_owner(scope)
        }
        if ambiguous_exact_identities:
            failure_count += len(ambiguous_exact_identities)
            logger.error(
                "Backup sync found %s ambiguous endpoint/cluster/VMID owner identity(s); "
                "their payloads and destructive cleanup are disabled: %s",
                len(ambiguous_exact_identities),
                sorted(ambiguous_exact_identities),
            )

        selected_owner_vmids: dict[tuple[int, str], set[str]] | None = None
        if netbox_vm_ids is not None:
            if not isinstance(vm_cache, _BackupVMCache):
                raise ProxboxException(
                    message="Unable to verify selected backup VM ownership",
                    detail="The selected VM cache did not preserve endpoint ownership.",
                    http_status_code=502,
                )
            selected_owner_vmids = {}
            selected_identity_keys: set[tuple[int, str, int]] = set()
            for scope in vm_cache.selected_scopes:
                identity_key = (scope.endpoint_id, scope.cluster_name, scope.proxmox_vmid)
                if identity_key in selected_identity_keys:
                    raise ProxboxException(
                        message="Unable to verify selected backup VM ownership",
                        detail=(
                            "Multiple selected NetBox VMs claim endpoint/cluster/VMID "
                            f"identity {identity_key}."
                        ),
                        http_status_code=502,
                    )
                selected_identity_keys.add(identity_key)
                selected_owner_vmids.setdefault((scope.endpoint_id, scope.cluster_name), set()).add(
                    str(scope.proxmox_vmid)
                )

            if not selected_owner_vmids:
                logger.info("Backup sync received an explicit empty VM selection")
                return results

            available_owner_keys = {
                (endpoint_id, cluster_name)
                for proxmox, cluster in zip(pxs, cluster_status)
                if (endpoint_id := extract_proxmox_session_endpoint_id(proxmox)) is not None
                and (cluster_name := _normalize_cluster_name(getattr(cluster, "name", None)))
                is not None
            }
            missing_owners = set(selected_owner_vmids) - available_owner_keys
            if missing_owners:
                raise ProxboxException(
                    message="Selected backup VM owner is unavailable",
                    detail=(
                        "No active Proxmox session/cluster pair exists for selected owner(s): "
                        f"{sorted(missing_owners)}."
                    ),
                    http_status_code=502,
                )

        all_raw_backups: list[dict] = []
        discovery_tasks: list[asyncio.Task] = []
        discovery_task_owners: list[tuple[int, str] | None] = []
        owner_discovery_ok: dict[tuple[int, str], bool] = {}
        fetch_semaphore = asyncio.Semaphore(fetch_max_concurrency or _resolve_fetch_concurrency())

        async def _discover_backups_for_node_storage(
            proxmox,
            endpoint_id: int | None,
            cluster_name: str,
            node_name: str,
            storage_name: str,
            allowed_vmids: set[str] | None,
        ) -> tuple[list[dict], set[str]]:
            effective_vmids = allowed_vmids if allowed_vmids is not None else selected_vmids
            if effective_vmids is not None and not effective_vmids:
                return [], set()
            async with fetch_semaphore:
                _extra: dict = {}
                if effective_vmids is not None and len(effective_vmids) == 1:
                    _extra["vmid"] = next(iter(effective_vmids))
                raw_backups = await get_node_storage_content(
                    proxmox,
                    node=node_name,
                    storage=storage_name,
                    content="backup",
                    **_extra,
                )
                backups = dump_models(raw_backups)
                if effective_vmids is not None:
                    backups = [
                        backup
                        for backup in backups
                        if str(backup.get("vmid", "")).strip() in effective_vmids
                    ]
                volids = _volids_from_proxmox_storage_backup_items(backups)
                filtered = []
                for backup in backups:
                    if backup.get("content") != "backup":
                        continue
                    annotated_backup = dict(backup)
                    annotated_backup[_BACKUP_ENDPOINT_ID_KEY] = endpoint_id
                    annotated_backup[_BACKUP_CLUSTER_NAME_KEY] = cluster_name
                    filtered.append(annotated_backup)
            return filtered, volids

        for proxmox, cluster in zip(pxs, cluster_status):
            cluster_name = getattr(cluster, "name", None) if cluster else None
            endpoint_id = extract_proxmox_session_endpoint_id(proxmox)
            owner_key = (
                endpoint_id,
                _normalize_cluster_name(cluster_name),
            )
            discovery_owner = (
                (endpoint_id, owner_key[1])
                if endpoint_id is not None and owner_key[1] is not None
                else None
            )
            allowed_vmids = None
            if selected_owner_vmids is not None:
                if owner_key not in selected_owner_vmids:
                    continue
                allowed_vmids = selected_owner_vmids[owner_key]
            if discovery_owner is not None:
                owner_discovery_ok.setdefault(discovery_owner, True)
            storage_payload = await resolve_async(proxmox.session.storage.get())
            storage_list = [
                {
                    "storage": storage_dict.get("storage"),
                    "nodes": storage_dict.get("nodes", "all"),
                }
                for storage_dict in storage_payload
                if "backup" in storage_dict.get("content")
            ]

            if discovery_owner is not None and not (cluster and cluster.node_list):
                owner_discovery_ok[discovery_owner] = False
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
                                        endpoint_id=endpoint_id,
                                        cluster_name=cluster_name,
                                        node_name=cluster_node.name,
                                        storage_name=storage.get("storage"),
                                        allowed_vmids=allowed_vmids,
                                    )
                                )
                            )
                            discovery_task_owners.append(discovery_owner)

        if discovery_tasks:
            discovery_results = await asyncio.gather(*discovery_tasks, return_exceptions=True)
            for owner, result in zip(discovery_task_owners, discovery_results):
                if isinstance(result, Exception):
                    failure_count += 1
                    if owner is not None:
                        owner_discovery_ok[owner] = False
                    logger.warning("Backup discovery failed: %s", result, exc_info=True)
                    continue
                node_backups, _node_volids = result
                all_raw_backups.extend(node_backups)

        cleanup_covered_vm_ids: set[int] = set()
        if isinstance(vm_cache, _BackupVMCache):
            for scope in vm_cache.selected_scopes:
                if owner_discovery_ok.get((scope.endpoint_id, scope.cluster_name)) is not True:
                    continue
                if not vm_cache.is_unambiguous_scope_owner(scope):
                    logger.warning(
                        "Suppressing stale backup cleanup for ambiguous VM ownership: "
                        "endpoint=%s cluster=%s vmid=%s netbox_vm_id=%s",
                        scope.endpoint_id,
                        scope.cluster_name,
                        scope.proxmox_vmid,
                        scope.netbox_vm_id,
                    )
                    continue
                cleanup_covered_vm_ids.add(scope.netbox_vm_id)

        all_payloads: list[dict] = []
        for backup in all_raw_backups:
            payload = compute_backup_payload(
                backup,
                vm_cache=vm_cache,
                storage_index=storage_index,
                cluster_name=backup.get(_BACKUP_CLUSTER_NAME_KEY),
                endpoint_id=backup.get(_BACKUP_ENDPOINT_ID_KEY),
            )
            if payload is not None:
                all_payloads.append(payload)

        proxmox_backup_owner_keys = {_backup_owner_key(payload) for payload in all_payloads}

        if not all_payloads:
            warning_msg = "No backups found to process"
            if use_websocket and websocket:
                await websocket.send_json(
                    {
                        "step": "backups",
                        "status": "warning",
                        "message": warning_msg,
                    }
                )
            # A complete empty discovery is meaningful when stale deletion is
            # enabled: skip reconciliation, but continue into owner-covered cleanup.
            logger.info("Backup sync: %s — skipping reconcile", warning_msg)
            if not delete_nonexistent_backup:
                return results
        else:
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
                _resolve_bulk_batch_size(),
                _resolve_bulk_batch_delay_ms(),
            )

            results, created_count, patched_count = await _bulk_reconcile_backups(
                nb,
                all_payloads,
            )

            logger.info(
                "Bulk reconcile completed: %s created, %s patched, %s total results",
                created_count,
                patched_count,
                len(results),
            )

        if delete_nonexistent_backup and failure_count:
            logger.warning(
                "Skipping backup deletion because %s Proxmox discovery scope(s) failed",
                failure_count,
            )
        elif delete_nonexistent_backup and cleanup_covered_vm_ids:
            try:
                netbox_backups = await rest_list_paginated_async(
                    nb,
                    "/api/plugins/proxbox/backups/",
                )
                ids_to_delete: list[int] = []
                skipped_no_volid = 0

                for backup in netbox_backups:
                    virtual_machine_id = _relation_id_or_none(backup.get("virtual_machine"))
                    if virtual_machine_id not in cleanup_covered_vm_ids:
                        continue
                    if selected_netbox_ids is not None:
                        if virtual_machine_id not in selected_netbox_ids:
                            continue
                    vid = backup.volume_id
                    if not vid:
                        skipped_no_volid += 1
                        continue
                    if (virtual_machine_id, str(vid)) not in proxmox_backup_owner_keys:
                        backup_id = backup.id
                        if backup_id is not None:
                            ids_to_delete.append(int(backup_id))

                if ids_to_delete:
                    # Delete backups one at a time. rest_bulk_delete_async detects single-item
                    # deletes and automatically uses detail-path DELETE (/{id}/) instead of
                    # query-param-based bulk delete, which accommodates plugin endpoints that
                    # don't support ?id= filters (like the proxbox backups endpoint).
                    for bid in ids_to_delete:
                        try:
                            deleted = await rest_bulk_delete_async(
                                nb,
                                "/api/plugins/proxbox/backups/",
                                [bid],
                            )
                            deleted_count += deleted
                        except Exception:
                            logger.warning(
                                "Failed to delete backup id=%s",
                                bid,
                                exc_info=True,
                            )
                            # Continue to next backup instead of aborting the batch

                        batch_delay_ms = _resolve_bulk_batch_delay_ms()
                        if batch_delay_ms > 0:
                            await asyncio.sleep(batch_delay_ms / 1000.0)

                if skipped_no_volid:
                    logger.info(
                        "Skipped %s NetBox backup(s) with empty volume_id",
                        skipped_no_volid,
                    )
            except Exception:
                logger.warning("Error during backup deletion pass", exc_info=True)
        elif delete_nonexistent_backup:
            logger.warning(
                "Skipping backup deletion because no unambiguous VM owner had complete discovery"
            )

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
    try:
        vm_ids = parse_selected_netbox_vm_ids(netbox_vm_ids)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return await _create_all_virtual_machine_backups(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        tag=tag,
        delete_nonexistent_backup=delete_nonexistent_backup,
        fetch_max_concurrency=fetch_max_concurrency,
        netbox_vm_ids=vm_ids,
    )


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
    try:
        vm_ids = parse_selected_netbox_vm_ids(netbox_vm_ids)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

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
                    netbox_vm_ids=vm_ids,
                    websocket=bridge,
                    use_websocket=True,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        async for frame in sse_stream_generator(bridge, sync_task, "backups"):
            yield frame

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
    vm_record = await netbox_session.virtualization.virtual_machines.get(id=netbox_vm_id)
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
                    netbox_vm_ids=[netbox_vm_id],
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        async for frame in sse_stream_generator(
            bridge,
            sync_task,
            "backups",
            started_message=f"Starting backup sync for VM id={netbox_vm_id}.",
        ):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
