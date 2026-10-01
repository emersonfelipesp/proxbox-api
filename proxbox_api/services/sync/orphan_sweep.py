"""Orphan cleanup for Proxbox-managed NetBox virtual machines."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any, TypeVar, cast

from proxbox_api.constants import (
    DISCOVERY_TAG_VM_LXC,
    DISCOVERY_TAG_VM_QEMU,
    SOFT_DELETE_TAG_COLOR,
    SOFT_DELETE_TAG_DESCRIPTION,
    SOFT_DELETE_TAG_NAME,
    SOFT_DELETE_TAG_SLUG,
)
from proxbox_api.enum.status_mapping import ProxmoxToNetBoxVMStatus
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_first_async, rest_patch_async
from proxbox_api.runtime_settings import get_bool
from proxbox_api.schemas.stream_messages import ErrorCategory, ItemOperation
from proxbox_api.services.sync.sync_state_reader import (
    SidecarVMOrphanScan,
    resolve_vm_sidecar_by_parent_id,
    scan_vm_sidecar_orphan_candidates,
)

VIRTUAL_MACHINES_PATH = "/api/virtualization/virtual-machines/"
VM_DISCOVERY_TAG_SLUGS: tuple[str, ...] = (DISCOVERY_TAG_VM_QEMU, DISCOVERY_TAG_VM_LXC)
ORPHAN_SWEEP_PHASE = "sweep_orphans"
SOFT_DELETED_VM_STATUS = "decommissioning"

SKIP_REASON_DISABLED = "disabled"
SKIP_REASON_VM_STAGE_FAILED = "vm_stage_failed"
SKIP_REASON_SIDECAR_UNAVAILABLE = "sidecar_unavailable"
SKIP_REASON_SIDECAR_READ_FAILED = "sidecar_read_failed"
SKIP_REASON_RUN_NOT_FOUND = "run_not_found"
SKIP_REASON_LIVE_INVENTORY_UNAVAILABLE = "live_inventory_unavailable"
SKIP_REASON_VM_UNREADABLE = "vm_unreadable"
SKIP_REASON_ALREADY_SWEPT = "already_swept"
ITEM_SKIP_STILL_PRESENT = "still_present_in_proxmox"
ITEM_SKIP_IDENTITY_INCOMPLETE = "identity_incomplete"

# (casefolded cluster name, Proxmox vmid, vm type "qemu"/"lxc")
LiveVmKey = tuple[str, int, str]

_RecordT = TypeVar("_RecordT")


def is_delete_orphans_enabled() -> bool:
    """Return whether the ``delete_orphans`` setting (env override first) is on."""
    return get_bool(
        settings_key="delete_orphans",
        env="PROXBOX_DELETE_ORPHANS",
        default=False,
    )


def _record_to_dict(record: object) -> dict[str, object] | None:
    if isinstance(record, dict):
        return cast(dict[str, object], record)
    for method_name in ("serialize", "dict"):
        method = getattr(record, method_name, None)
        if callable(method):
            try:
                value = method()
            except Exception as error:
                logger.debug("Failed to coerce VM record during orphan sweep: %s", error)
                return None
            return cast(dict[str, object], value) if isinstance(value, dict) else None
    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if value is None:
        return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _tag_slugs(record: dict[str, object]) -> list[str]:
    tags = record.get("tags")
    if not isinstance(tags, list):
        return []
    slugs: list[str] = []
    for tag in tags:
        if isinstance(tag, dict):
            tag_dict = cast(dict[str, object], tag)
            raw = tag_dict.get("slug") or tag_dict.get("name")
        else:
            raw = tag
        if raw:
            slugs.append(str(raw))
    return sorted(dict.fromkeys(slugs))


def _live_key(cluster: object, vmid: object, vm_type: object) -> LiveVmKey | None:
    cluster_name = str(cluster or "").strip().casefold()
    vm_id = _coerce_int(vmid)
    kind = str(vm_type or "").strip().lower()
    if not cluster_name or vm_id is None or kind not in {"qemu", "lxc"}:
        return None
    return (cluster_name, vm_id, kind)


class LiveInventoryError(ValueError):
    """Raised when a guest resource row cannot be identified, so the inventory is unsafe."""


_GUEST_TYPES = frozenset({"qemu", "lxc"})


def _guest_row_identity(data: dict[str, object]) -> tuple[str, object] | None:
    """Return ``(type, vmid)`` for a guest row, or ``None`` for a non-guest row.

    Missing ``type``/``vmid`` fields are derived from an ``id`` like ``qemu/123``. A row
    that is guest-shaped but cannot be fully identified raises ``LiveInventoryError``.
    """
    raw_id = str(data.get("id") or "").strip().lower()
    id_type, _, id_vmid = raw_id.partition("/")
    id_type = id_type if id_type in _GUEST_TYPES else ""
    kind = str(data.get("type") or "").strip().lower() or id_type
    if kind not in _GUEST_TYPES:
        return None
    vmid = data.get("vmid")
    if _coerce_int(vmid) is None and id_type == kind:
        vmid = id_vmid
    if _coerce_int(vmid) is None:
        raise LiveInventoryError(f"guest resource row of type {kind!r} has no usable vmid")
    return kind, vmid


def build_live_vm_keys(cluster_resources: object) -> frozenset[LiveVmKey]:
    """Build guest identity keys from live cluster resources.

    ``cluster_resources`` is the ``[{cluster_name: [resource, ...]}, ...]`` shape the
    cluster resources route returns. Non-guest resources (storage, node, sdn, pool) are
    ignored. A guest row whose type or vmid cannot be determined (fields are derived
    from ``id`` when missing) raises ``LiveInventoryError`` so callers treat the whole
    inventory as unavailable instead of letting a present guest look absent.
    """
    keys: set[LiveVmKey] = set()
    if not isinstance(cluster_resources, (list, tuple)):
        return frozenset()
    for entry in cluster_resources:
        if not isinstance(entry, dict):
            continue
        for cluster_name, resources in entry.items():
            if not isinstance(resources, (list, tuple)):
                continue
            for resource in resources:
                data = _record_to_dict(resource)
                identity = _guest_row_identity(data) if data is not None else None
                if identity is None:
                    continue
                key = _live_key(cluster_name, identity[1], identity[0])
                if key is None:
                    raise LiveInventoryError("guest resource row cannot be identified")
                keys.add(key)
    return frozenset(keys)


def _live_presence_skip_reason(
    candidate: dict[str, object],
    live_vm_keys: Collection[LiveVmKey] | None,
) -> str | None:
    """Explain why a candidate must not be soft-deleted given the live inventory."""
    if live_vm_keys is None:
        return None
    key = _live_key(
        candidate.get("_proxmox_cluster_name"),
        candidate.get("_proxmox_vm_id"),
        candidate.get("_proxmox_vm_type"),
    )
    if key is None:
        return ITEM_SKIP_IDENTITY_INCOMPLETE
    return ITEM_SKIP_STILL_PRESENT if key in live_vm_keys else None


def extract_touched_vm_ids(value: object) -> set[int]:
    """Extract NetBox VM IDs from nested sync result payloads."""
    touched: set[int] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            item_dict = cast(dict[str, object], item)
            record_id = _coerce_int(item_dict.get("id") or item_dict.get("netbox_id"))
            if record_id is not None:
                touched.add(record_id)
            for key in ("virtual_machine", "vm", "netbox_object"):
                if key in item_dict:
                    visit(item_dict[key])
        elif isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child)
        else:
            record = _record_to_dict(item)
            if record is not None:
                visit(record)

    visit(value)
    return touched


def _normalized_vm_slugs(vm_slugs: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(slug) for slug in vm_slugs if str(slug).strip()))


async def _add_sidecar_orphan_candidates(
    nb: object,
    *,
    run_id: str,
    vm_slugs: tuple[str, ...],
    endpoint_ids: Collection[int] | None,
    candidates_by_id: dict[int, dict[str, object]],
) -> SidecarVMOrphanScan:
    scan = await scan_vm_sidecar_orphan_candidates(
        nb,
        run_id=run_id,
        vm_slugs=vm_slugs,
        endpoint_ids=endpoint_ids,
    )
    if scan is None:
        return SidecarVMOrphanScan(
            stale_candidates=[],
            current_vm_ids=set(),
            sidecar_unavailable=True,
        )
    if scan.sidecar_read_failed:
        logger.warning(
            "Skipping orphan VM sweep for run_id=%s because "
            "the VM sidecar scan failed transiently; refusing to soft-delete VMs whose "
            "current sidecar state cannot be verified",
            run_id,
        )
        return scan
    for candidate in scan.stale_candidates:
        record_id = _coerce_int(candidate.get("id"))
        if record_id is not None:
            candidates_by_id.setdefault(record_id, candidate)
    return scan


def _scan_skip_reason(scan: SidecarVMOrphanScan) -> str | None:
    if scan.sidecar_read_failed:
        return SKIP_REASON_SIDECAR_READ_FAILED
    if scan.sidecar_unavailable:
        return SKIP_REASON_SIDECAR_UNAVAILABLE
    if not scan.run_seen_in_scope:
        return SKIP_REASON_RUN_NOT_FOUND
    return None


class OrphanCandidateList(list[dict[str, object]]):
    """Orphan candidates plus the reason the sidecar scan could not be trusted.

    ``skipped_reason`` is ``None`` when the scan succeeded. An empty list with a
    reason means "could not verify", which callers must not treat as "no orphans".
    """

    def __init__(
        self,
        values: Iterable[dict[str, object]] = (),
        *,
        skipped_reason: str | None = None,
    ) -> None:
        super().__init__(values)
        self.skipped_reason = skipped_reason


async def find_orphan_vms(
    nb: object,
    run_id: str,
    *,
    vm_slugs: Iterable[str] = VM_DISCOVERY_TAG_SLUGS,
    endpoint_ids: Collection[int] | None = None,
) -> list[dict[str, object]]:
    """Find Proxbox-discovered VMs not touched by the current run.

    ``endpoint_ids`` limits candidates to sidecars owned by those Proxmox endpoints;
    ``None`` scans every endpoint, and an empty collection matches nothing. The
    returned list is an ``OrphanCandidateList`` whose ``skipped_reason`` is set when
    the sidecar scan was unavailable or failed.
    """
    if not run_id:
        raise ValueError("run_id is required for orphan VM discovery")

    candidates_by_id: dict[int, dict[str, object]] = {}
    sidecar_scan = await _add_sidecar_orphan_candidates(
        nb,
        run_id=run_id,
        vm_slugs=_normalized_vm_slugs(vm_slugs),
        endpoint_ids=endpoint_ids,
        candidates_by_id=candidates_by_id,
    )
    return OrphanCandidateList(
        candidates_by_id.values(),
        skipped_reason=_scan_skip_reason(sidecar_scan),
    )


def _candidate_item(candidate: dict[str, object], *, run_id: str) -> dict[str, object]:
    return {
        "name": str(candidate.get("name") or candidate.get("display") or candidate.get("id")),
        "type": "virtual_machine",
        "netbox_id": _coerce_int(candidate.get("id")),
        "netbox_url": candidate.get("display_url") or candidate.get("url"),
        "extra": {
            "reason": "orphan",
            "run_id": run_id,
            "stale_run_id": candidate.get("_proxbox_last_run_id"),
            "tag_slugs": _tag_slugs(candidate),
            "vmid": candidate.get("_proxmox_vm_id"),
        },
    }


def _item_extra(item: dict[str, object]) -> dict[str, object]:
    extra = item.get("extra")
    return cast(dict[str, object], extra) if isinstance(extra, dict) else {}


def _is_not_found_error(error: Exception) -> bool:
    if isinstance(error, ProxboxException):
        text = " ".join(
            str(part)
            for part in (getattr(error, "message", ""), getattr(error, "detail", ""))
            if part
        ).lower()
    else:
        text = str(error).lower()
    return "404" in text or "not found" in text


async def _emit_summary(
    stream: object | None,
    *,
    soft_deleted: int,
    failed: int,
    skipped: int,
    message: str,
) -> None:
    if stream is None:
        return
    emit_phase_summary = getattr(stream, "emit_phase_summary", None)
    if not callable(emit_phase_summary):
        return
    await emit_phase_summary(
        phase=ORPHAN_SWEEP_PHASE,
        updated=soft_deleted,
        failed=failed,
        skipped=skipped,
        message=message,
    )


async def _emit_item_progress(
    stream: object | None,
    *,
    item: dict[str, object],
    operation: ItemOperation,
    status: str,
    message: str,
    progress_current: int,
    progress_total: int,
    error: str | None = None,
    warning: str | None = None,
) -> None:
    if stream is None:
        return
    emit_item_progress = getattr(stream, "emit_item_progress", None)
    if not callable(emit_item_progress):
        return
    await emit_item_progress(
        phase=ORPHAN_SWEEP_PHASE,
        item=item,
        operation=operation,
        status=status,
        message=message,
        progress_current=progress_current,
        progress_total=progress_total,
        error=error,
        warning=warning,
    )


async def _abort_for_touched_candidates(
    candidates: list[dict[str, object]],
    *,
    run_id: str,
    touched_vm_ids: set[int],
    stream: object | None,
) -> None:
    invalid = [
        candidate
        for candidate in candidates
        if (record_id := _coerce_int(candidate.get("id"))) is not None
        and record_id in touched_vm_ids
    ]
    if not invalid:
        return

    names = ", ".join(str(candidate.get("name") or candidate.get("id")) for candidate in invalid)
    detail = (
        "Refusing to sweep orphan VMs because candidates were also touched "
        f"by this run_id={run_id}: {names}"
    )
    emit_error_detail = getattr(stream, "emit_error_detail", None) if stream is not None else None
    if callable(emit_error_detail):
        await emit_error_detail(
            message="Orphan VM sweep invariant failed",
            category=ErrorCategory.INTERNAL,
            phase=ORPHAN_SWEEP_PHASE,
            detail=detail,
            suggestion="Check proxbox_last_run_id stamping before enabling orphan soft deletion.",
        )
    raise ProxboxException(message="Orphan VM sweep invariant failed", detail=detail)


def _tag_slug(value: object) -> str | None:
    if isinstance(value, dict):
        raw = value.get("slug") or value.get("name")
    else:
        raw = getattr(value, "slug", None) or getattr(value, "name", None)
    normalized = str(raw or "").strip().lower()
    return normalized or None


def _tag_ref(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        tag_id = _coerce_int(value.get("id"))
        if tag_id is not None:
            return {"id": tag_id}
        slug = _tag_slug(value)
        return {"slug": slug} if slug else None
    tag_id = _coerce_int(getattr(value, "id", None))
    if tag_id is not None:
        return {"id": tag_id}
    slug = _tag_slug(value)
    return {"slug": slug} if slug else None


def _soft_delete_tag_refs(
    candidate: dict[str, object],
    *,
    soft_delete_tag_id: int | None,
) -> list[dict[str, object]]:
    tags = candidate.get("tags")
    refs: list[dict[str, object]] = []
    seen: set[str] = set()
    if isinstance(tags, list):
        for tag in tags:
            ref = _tag_ref(tag)
            slug = _tag_slug(tag)
            if ref is None:
                continue
            key = f"id:{ref['id']}" if "id" in ref else f"slug:{slug}"
            if key not in seen:
                refs.append(ref)
                seen.add(key)
            if slug == SOFT_DELETE_TAG_SLUG:
                seen.add(f"slug:{SOFT_DELETE_TAG_SLUG}")

    marker_ref = (
        {"id": soft_delete_tag_id}
        if soft_delete_tag_id is not None
        else {"slug": SOFT_DELETE_TAG_SLUG}
    )
    marker_key = (
        f"id:{soft_delete_tag_id}"
        if soft_delete_tag_id is not None
        else f"slug:{SOFT_DELETE_TAG_SLUG}"
    )
    if marker_key not in seen:
        refs.append(marker_ref)
    return refs


def _has_soft_delete_marker(candidate: dict[str, object]) -> bool:
    tags = candidate.get("tags")
    return isinstance(tags, list) and any(_tag_slug(tag) == SOFT_DELETE_TAG_SLUG for tag in tags)


def _status_value(record: dict[str, object]) -> str:
    status = record.get("status")
    raw = status.get("value") if isinstance(status, dict) else getattr(status, "value", status)
    return str(raw or "").strip().lower()


def is_soft_deleted_vm(record: object) -> bool:
    """Return whether a NetBox VM record is decommissioned or carries the orphan marker.

    Accepts dicts and SDK records; ``status`` may be a nested ``{"value": ...}`` object
    or a plain string, and ``tags`` may hold dicts or plain slugs.
    """
    data = _record_to_dict(record)
    if data is None:
        return False
    if _status_value(data) == SOFT_DELETED_VM_STATUS:
        return True
    return _has_soft_delete_marker(data) or SOFT_DELETE_TAG_SLUG in _tag_slugs(data)


def exclude_soft_deleted_vms(records: Iterable[_RecordT], *, stage: str) -> list[_RecordT]:
    """Drop decommissioned or soft-deleted VMs so a stage never syncs them."""
    kept: list[_RecordT] = []
    excluded = 0
    for record in records:
        if is_soft_deleted_vm(record):
            excluded += 1
        else:
            kept.append(record)
    if excluded:
        logger.info(
            "Skipping %d decommissioned or soft-deleted VM(s) during %s sync",
            excluded,
            stage,
        )
    return kept


def reappeared_vm_status(desired_status: object) -> str:
    """Map the Proxmox-derived desired status to the NetBox status to restore."""
    return ProxmoxToNetBoxVMStatus.from_proxmox(desired_status).value


async def clear_soft_delete_marker(
    nb: object,
    virtual_machine: object,
    *,
    restored_status: str | None = None,
) -> object:
    """Remove the orphan marker when a VM is successfully re-adopted.

    ``restored_status`` is written in the same PATCH when the VM is still
    ``decommissioning``. The bulk reconciliation diff normalizes every unknown NetBox
    status to ``active``, so it cannot notice and undo the sweep's status change.
    """
    record = _record_to_dict(virtual_machine)
    if record is None or not _has_soft_delete_marker(record):
        return virtual_machine
    record_id = _coerce_int(record.get("id"))
    tags = record.get("tags")
    if record_id is None or not isinstance(tags, list):
        raise ProxboxException(
            message="Cannot clear the soft-delete marker from a re-synced VM.",
            detail="The reconciled NetBox VM did not include a valid ID and tag list.",
        )
    refs = [
        ref
        for tag in tags
        if _tag_slug(tag) != SOFT_DELETE_TAG_SLUG
        if (ref := _tag_ref(tag)) is not None
    ]
    payload: dict[str, object] = {"tags": refs}
    if restored_status and _status_value(record) == SOFT_DELETED_VM_STATUS:
        payload["status"] = restored_status
    await rest_patch_async(nb, VIRTUAL_MACHINES_PATH, record_id, payload)
    return virtual_machine


async def _no_longer_stale_reason(
    nb: object,
    candidate: dict[str, object],
    *,
    record_id: int,
    run_id: str,
) -> str | None:
    """Re-read the VM sidecar right before a PATCH and explain why it is unsafe to mark.

    Returns ``None`` only when the sidecar still carries the same stale ``last_run_id``
    the discovery pass saw. NetBox offers no compare-and-set, so this narrows the
    discovery-to-PATCH race window but cannot close it atomically.
    """
    sidecar = await resolve_vm_sidecar_by_parent_id(nb, record_id)
    if sidecar is None:
        return "its sync-state sidecar could not be re-read"
    current = str(sidecar.get("last_run_id") or "").strip()
    if current == run_id:
        return "it was restamped by this run after discovery"
    seen = candidate.get("_proxbox_last_run_id")
    if seen is not None and current != str(seen or "").strip():
        return "its sync-state changed after discovery"
    return None


async def _fresh_vm_record(nb: object, record_id: int) -> dict[str, object] | None:
    """Re-read one NetBox VM right before its PATCH; ``None`` when it cannot be read."""
    try:
        record = await rest_first_async(
            nb, VIRTUAL_MACHINES_PATH, query={"id": record_id, "limit": 2}
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Cannot re-read VM id=%s before soft-delete: %s", record_id, error)
        return None
    data = _record_to_dict(record) if record is not None else None
    if data is None or _coerce_int(data.get("id")) != record_id:
        return None
    return data


async def _fresh_tag_refs(
    nb: object, record_id: int, *, soft_delete_tag_id: int | None
) -> tuple[list[dict[str, object]] | None, str | None]:
    """Build the PATCH tag list from the freshly read VM tags plus the marker.

    Returns ``(refs, None)`` or ``(None, reason)`` with reason ``vm_unreadable`` or
    ``already_soft_deleted``. NetBox has no atomic tag add, so a tiny window between
    this read and the PATCH remains where a concurrently added tag can still be lost.
    """
    fresh = await _fresh_vm_record(nb, record_id)
    if fresh is None:
        return None, SKIP_REASON_VM_UNREADABLE
    if _has_soft_delete_marker(fresh):
        return None, SKIP_REASON_ALREADY_SWEPT
    return _soft_delete_tag_refs(fresh, soft_delete_tag_id=soft_delete_tag_id), None


async def _skip_live_candidate(
    stream: object | None,
    *,
    item: dict[str, object],
    reason: str,
    run_id: str,
    progress_current: int,
    progress_total: int,
) -> None:
    name = str(item["name"])
    logger.warning(
        "Skipping orphan VM '%s' (netbox_id=%s) run_id=%s: %s",
        name,
        item.get("netbox_id"),
        run_id,
        reason,
    )
    await _emit_item_progress(
        stream,
        item=item,
        operation=ItemOperation.SKIPPED,
        status="skipped",
        message=f"Skipped orphan VM '{name}': {reason}",
        progress_current=progress_current,
        progress_total=progress_total,
        warning=reason,
    )


async def soft_delete_orphan_vms(
    nb: object,
    candidates: Iterable[dict[str, object]],
    *,
    run_id: str,
    dry_run: bool = False,
    stream: object | None = None,
    touched_vm_ids: set[int] | None = None,
    soft_delete_tag_id: int | None = None,
    live_vm_keys: Collection[LiveVmKey] | None = None,
) -> dict[str, object]:
    """Soft-delete or preview stale Proxbox-managed VMs.

    When ``live_vm_keys`` is given, a candidate is marked only if its guest is confirmed
    absent from that live inventory; present or unidentifiable guests are skipped.
    """
    candidate_list = list(candidates)
    await _abort_for_touched_candidates(
        candidate_list,
        run_id=run_id,
        touched_vm_ids=touched_vm_ids or set(),
        stream=stream,
    )

    soft_deleted = 0
    failed = 0
    skipped = 0
    total = len(candidate_list)

    for index, candidate in enumerate(candidate_list, start=1):
        record_id = _coerce_int(candidate.get("id"))
        item = _candidate_item(candidate, run_id=run_id)
        item_extra = _item_extra(item)
        name = str(item["name"])

        if record_id is None:
            skipped += 1
            await _emit_item_progress(
                stream,
                item=item,
                operation=ItemOperation.SKIPPED,
                status="skipped",
                message=f"Skipped orphan VM '{name}' because it has no NetBox ID",
                progress_current=index,
                progress_total=total,
                warning="Missing NetBox object ID",
            )
            continue

        live_reason = _live_presence_skip_reason(candidate, live_vm_keys)
        if live_reason is not None:
            skipped += 1
            await _skip_live_candidate(
                stream,
                item=item,
                reason=live_reason,
                run_id=run_id,
                progress_current=index,
                progress_total=total,
            )
            continue

        if dry_run:
            skipped += 1
            await _emit_item_progress(
                stream,
                item=item,
                operation=ItemOperation.WOULD_DELETE,
                status="completed",
                message=f"Would soft-delete orphan VM '{name}'",
                progress_current=index,
                progress_total=total,
            )
            continue

        changed_reason = await _no_longer_stale_reason(
            nb, candidate, record_id=record_id, run_id=run_id
        )
        if changed_reason is not None:
            skipped += 1
            logger.warning(
                "Skipping orphan VM id=%s run_id=%s: %s", record_id, run_id, changed_reason
            )
            await _emit_item_progress(
                stream,
                item=item,
                operation=ItemOperation.SKIPPED,
                status="skipped",
                message=f"Skipped orphan VM '{name}' because {changed_reason}",
                progress_current=index,
                progress_total=total,
                warning=f"restamped: {changed_reason}",
            )
            continue

        tag_refs, tag_skip = await _fresh_tag_refs(
            nb, record_id, soft_delete_tag_id=soft_delete_tag_id
        )
        if tag_refs is None:
            skipped += 1
            await _skip_live_candidate(
                stream,
                item=item,
                reason=str(tag_skip),
                run_id=run_id,
                progress_current=index,
                progress_total=total,
            )
            continue

        try:
            await rest_patch_async(
                nb,
                VIRTUAL_MACHINES_PATH,
                record_id,
                {"status": "decommissioning", "tags": tag_refs},
            )
        except Exception as error:
            if _is_not_found_error(error):
                skipped += 1
                await _emit_item_progress(
                    stream,
                    item=item,
                    operation=ItemOperation.SKIPPED,
                    status="skipped",
                    message=f"Skipped orphan VM '{name}' because it was already gone",
                    progress_current=index,
                    progress_total=total,
                    warning=str(error),
                )
                continue

            failed += 1
            logger.exception(
                "Failed to soft-delete orphan VM id=%s name=%s run_id=%s stale_run_id=%s",
                record_id,
                name,
                run_id,
                item_extra.get("stale_run_id"),
            )
            await _emit_item_progress(
                stream,
                item=item,
                operation=ItemOperation.FAILED,
                status="failed",
                message=f"Failed to soft-delete orphan VM '{name}'",
                progress_current=index,
                progress_total=total,
                error=str(error),
            )
            await _emit_summary(
                stream,
                soft_deleted=soft_deleted,
                failed=failed,
                skipped=skipped,
                message=(
                    f"Orphan VM soft-delete sweep failed after marking {soft_deleted} of {total} candidate(s)"
                ),
            )
            raise ProxboxException(
                message="Error while sweeping orphan virtual machines.",
                detail=str(error),
            ) from error

        soft_deleted += 1
        logger.info(
            "Soft-deleted orphan VM id=%s name=%s run_id=%s stale_run_id=%s tag_slugs=%s",
            record_id,
            name,
            run_id,
            item_extra.get("stale_run_id"),
            item_extra.get("tag_slugs"),
        )
        await _emit_item_progress(
            stream,
            item=item,
            operation=ItemOperation.UPDATED,
            status="completed",
            message=f"Soft-deleted orphan VM '{name}'",
            progress_current=index,
            progress_total=total,
        )

    message = (
        f"Orphan VM soft-delete dry-run completed: {total} candidate(s), 0 marked"
        if dry_run
        else f"Orphan VM sweep completed: {soft_deleted} soft-deleted, {skipped} skipped"
    )
    await _emit_summary(
        stream,
        soft_deleted=soft_deleted,
        failed=failed,
        skipped=skipped,
        message=message,
    )
    return {
        "run_id": run_id,
        "dry_run": dry_run,
        "candidates": total,
        "deleted": 0,
        "soft_deleted": soft_deleted,
        "failed": failed,
        "skipped": skipped,
    }


def _sweep_result(
    run_id: str,
    *,
    enabled: bool,
    dry_run: bool = False,
    skipped_reason: str | None = None,
) -> dict[str, object]:
    return {
        "enabled": enabled,
        "run_id": run_id,
        "dry_run": dry_run,
        "candidates": 0,
        "deleted": 0,
        "soft_deleted": 0,
        "failed": 0,
        "skipped": 0,
        "skipped_reason": skipped_reason,
    }


async def _skipped_sweep_result(
    run_id: str,
    *,
    enabled: bool,
    dry_run: bool,
    stream: object | None,
    reason: str,
) -> dict[str, object]:
    logger.warning("Orphan VM sweep skipped for run_id=%s: %s", run_id, reason)
    await _emit_summary(
        stream,
        soft_deleted=0,
        failed=0,
        skipped=0,
        message=f"Orphan VM sweep skipped: {reason}",
    )
    return _sweep_result(run_id, enabled=enabled, dry_run=dry_run, skipped_reason=reason)


async def _ensure_soft_delete_tag_id(nb: object) -> int:
    from proxbox_api.netbox_rest import ensure_tag_async

    marker = await ensure_tag_async(
        nb,
        name=SOFT_DELETE_TAG_NAME,
        slug=SOFT_DELETE_TAG_SLUG,
        color=SOFT_DELETE_TAG_COLOR,
        description=SOFT_DELETE_TAG_DESCRIPTION,
    )
    tag_id = _coerce_int(getattr(marker, "id", None))
    if tag_id is None and isinstance(marker, dict):
        tag_id = _coerce_int(marker.get("id"))
    if tag_id is None:
        raise ProxboxException(
            message="Cannot soft-delete orphan VMs because the marker tag has no ID.",
            detail="NetBox returned an unusable soft-delete tag record.",
        )
    return tag_id


async def run_orphan_vm_sweep(
    nb: object,
    *,
    run_id: str,
    enabled: bool,
    dry_run: bool = False,
    stream: object | None = None,
    touched_vm_ids: set[int] | None = None,
    endpoint_ids: Collection[int] | None = None,
    vm_stage_failed: bool = False,
    live_vm_keys: Collection[LiveVmKey] | None = None,
    live_inventory_unavailable: bool = False,
) -> dict[str, object]:
    """Run the orphan VM sweep when enabled or preview it in dry-run mode.

    ``endpoint_ids`` restricts the sweep to VMs owned by those Proxmox endpoints so an
    endpoint-limited run never marks another endpoint's VMs. ``vm_stage_failed`` skips
    the sweep because a VM that failed to reconcile was not stamped with this run and
    would otherwise look orphaned. ``live_vm_keys`` (see ``build_live_vm_keys``) is the
    backend-verified live guest inventory: candidates still present in it are never
    marked; ``live_inventory_unavailable`` skips the sweep because that inventory could
    not be fetched for every in-scope session. Every result carries ``skipped_reason`` (``None``
    when the sweep ran).
    """
    if not enabled and not dry_run:
        return _sweep_result(run_id, enabled=False, skipped_reason=SKIP_REASON_DISABLED)

    if vm_stage_failed:
        return await _skipped_sweep_result(
            run_id,
            enabled=enabled,
            dry_run=dry_run,
            stream=stream,
            reason=SKIP_REASON_VM_STAGE_FAILED,
        )

    if live_inventory_unavailable:
        return await _skipped_sweep_result(
            run_id,
            enabled=enabled,
            dry_run=dry_run,
            stream=stream,
            reason=SKIP_REASON_LIVE_INVENTORY_UNAVAILABLE,
        )

    candidates = await find_orphan_vms(nb, run_id, endpoint_ids=endpoint_ids)
    scan_skip_reason = getattr(candidates, "skipped_reason", None)
    if scan_skip_reason is not None:
        return await _skipped_sweep_result(
            run_id,
            enabled=enabled,
            dry_run=dry_run,
            stream=stream,
            reason=scan_skip_reason,
        )
    soft_delete_tag_id = (
        await _ensure_soft_delete_tag_id(nb) if candidates and not dry_run else None
    )
    result = await soft_delete_orphan_vms(
        nb,
        candidates,
        run_id=run_id,
        dry_run=dry_run,
        stream=stream,
        touched_vm_ids=touched_vm_ids,
        soft_delete_tag_id=soft_delete_tag_id,
        live_vm_keys=live_vm_keys,
    )
    return {"enabled": enabled, "skipped_reason": None, **result}
