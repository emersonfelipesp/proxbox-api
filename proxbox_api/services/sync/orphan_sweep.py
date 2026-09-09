"""Orphan cleanup for Proxbox-managed NetBox virtual machines."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from proxbox_api.constants import (
    DISCOVERY_TAG_VM_LXC,
    DISCOVERY_TAG_VM_QEMU,
    SOFT_DELETE_TAG_COLOR,
    SOFT_DELETE_TAG_DESCRIPTION,
    SOFT_DELETE_TAG_NAME,
    SOFT_DELETE_TAG_SLUG,
)
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_patch_async
from proxbox_api.schemas.stream_messages import ErrorCategory, ItemOperation
from proxbox_api.services.sync.sync_state_reader import (
    SidecarVMOrphanScan,
    scan_vm_sidecar_orphan_candidates,
)

VIRTUAL_MACHINES_PATH = "/api/virtualization/virtual-machines/"
VM_DISCOVERY_TAG_SLUGS: tuple[str, ...] = (DISCOVERY_TAG_VM_QEMU, DISCOVERY_TAG_VM_LXC)
ORPHAN_SWEEP_PHASE = "sweep_orphans"


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
    candidates_by_id: dict[int, dict[str, object]],
) -> SidecarVMOrphanScan:
    scan = await scan_vm_sidecar_orphan_candidates(
        nb,
        run_id=run_id,
        vm_slugs=vm_slugs,
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


async def find_orphan_vms(
    nb: object,
    run_id: str,
    *,
    vm_slugs: Iterable[str] = VM_DISCOVERY_TAG_SLUGS,
) -> list[dict[str, object]]:
    """Find Proxbox-discovered VMs not touched by the current run."""
    if not run_id:
        raise ValueError("run_id is required for orphan VM discovery")

    candidates_by_id: dict[int, dict[str, object]] = {}
    normalized_slugs = _normalized_vm_slugs(vm_slugs)
    sidecar_scan = await _add_sidecar_orphan_candidates(
        nb,
        run_id=run_id,
        vm_slugs=normalized_slugs,
        candidates_by_id=candidates_by_id,
    )
    if sidecar_scan.sidecar_read_failed:
        return list(candidates_by_id.values())
    return list(candidates_by_id.values())


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


async def clear_soft_delete_marker(nb: object, virtual_machine: object) -> object:
    """Remove the orphan marker when a VM is successfully re-adopted."""
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
    await rest_patch_async(nb, VIRTUAL_MACHINES_PATH, record_id, {"tags": refs})
    return virtual_machine


async def soft_delete_orphan_vms(
    nb: object,
    candidates: Iterable[dict[str, object]],
    *,
    run_id: str,
    dry_run: bool = False,
    stream: object | None = None,
    touched_vm_ids: set[int] | None = None,
    soft_delete_tag_id: int | None = None,
) -> dict[str, object]:
    """Soft-delete or preview stale Proxbox-managed VMs."""
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

        try:
            await rest_patch_async(
                nb,
                VIRTUAL_MACHINES_PATH,
                record_id,
                {
                    "status": "decommissioning",
                    "tags": _soft_delete_tag_refs(
                        candidate,
                        soft_delete_tag_id=soft_delete_tag_id,
                    ),
                },
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


async def run_orphan_vm_sweep(
    nb: object,
    *,
    run_id: str,
    enabled: bool,
    dry_run: bool = False,
    stream: object | None = None,
    touched_vm_ids: set[int] | None = None,
) -> dict[str, object]:
    """Run the orphan VM sweep when enabled or preview it in dry-run mode."""
    if not enabled and not dry_run:
        return {
            "enabled": False,
            "run_id": run_id,
            "dry_run": False,
            "candidates": 0,
            "deleted": 0,
            "soft_deleted": 0,
            "failed": 0,
            "skipped": 0,
        }

    candidates = await find_orphan_vms(nb, run_id)
    soft_delete_tag_id: int | None = None
    if candidates and not dry_run:
        from proxbox_api.netbox_rest import ensure_tag_async

        marker = await ensure_tag_async(
            nb,
            name=SOFT_DELETE_TAG_NAME,
            slug=SOFT_DELETE_TAG_SLUG,
            color=SOFT_DELETE_TAG_COLOR,
            description=SOFT_DELETE_TAG_DESCRIPTION,
        )
        soft_delete_tag_id = _coerce_int(getattr(marker, "id", None))
        if soft_delete_tag_id is None and isinstance(marker, dict):
            soft_delete_tag_id = _coerce_int(marker.get("id"))
        if soft_delete_tag_id is None:
            raise ProxboxException(
                message="Cannot soft-delete orphan VMs because the marker tag has no ID.",
                detail="NetBox returned an unusable soft-delete tag record.",
            )
    result = await soft_delete_orphan_vms(
        nb,
        candidates,
        run_id=run_id,
        dry_run=dry_run,
        stream=stream,
        touched_vm_ids=touched_vm_ids,
        soft_delete_tag_id=soft_delete_tag_id,
    )
    return {"enabled": enabled, **result}
