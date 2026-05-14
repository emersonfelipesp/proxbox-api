"""Orphan cleanup for Proxbox-managed NetBox virtual machines."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from proxbox_api.constants import DISCOVERY_TAG_VM_LXC, DISCOVERY_TAG_VM_QEMU
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_bulk_delete_async, rest_list_paginated_async
from proxbox_api.schemas.stream_messages import ErrorCategory, ItemOperation
from proxbox_api.services.sync.vm_helpers import LAST_RUN_ID_CUSTOM_FIELD

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


def _custom_fields(record: dict[str, object]) -> dict[str, object]:
    value = record.get("custom_fields")
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


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
    run_filter = f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__nie"
    empty_filter = f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__empty"

    for slug in dict.fromkeys(str(slug) for slug in vm_slugs if str(slug).strip()):
        queries: tuple[dict[str, object], dict[str, object]] = (
            {"tag": slug, run_filter: run_id},
            {"tag": slug, empty_filter: True},
        )
        for query in queries:
            records = await rest_list_paginated_async(
                nb,
                VIRTUAL_MACHINES_PATH,
                base_query=query,
                page_size=200,
            )
            for record in records:
                data = _record_to_dict(record)
                if data is None:
                    continue
                record_id = _coerce_int(data.get("id"))
                if record_id is None:
                    continue
                candidates_by_id.setdefault(record_id, data)

    return list(candidates_by_id.values())


def _candidate_item(candidate: dict[str, object], *, run_id: str) -> dict[str, object]:
    custom_fields = _custom_fields(candidate)
    return {
        "name": str(candidate.get("name") or candidate.get("display") or candidate.get("id")),
        "type": "virtual_machine",
        "netbox_id": _coerce_int(candidate.get("id")),
        "netbox_url": candidate.get("display_url") or candidate.get("url"),
        "extra": {
            "reason": "orphan",
            "run_id": run_id,
            "stale_run_id": custom_fields.get(LAST_RUN_ID_CUSTOM_FIELD),
            "tag_slugs": _tag_slugs(candidate),
            "vmid": custom_fields.get("proxmox_vm_id"),
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
    deleted: int,
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
        deleted=deleted,
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
            suggestion="Check proxbox_last_run_id stamping before enabling orphan deletion.",
        )
    raise ProxboxException(message="Orphan VM sweep invariant failed", detail=detail)


async def delete_orphan_vms(
    nb: object,
    candidates: Iterable[dict[str, object]],
    *,
    run_id: str,
    dry_run: bool = False,
    stream: object | None = None,
    touched_vm_ids: set[int] | None = None,
) -> dict[str, object]:
    """Delete or preview stale Proxbox-managed VMs."""
    candidate_list = list(candidates)
    await _abort_for_touched_candidates(
        candidate_list,
        run_id=run_id,
        touched_vm_ids=touched_vm_ids or set(),
        stream=stream,
    )

    deleted = 0
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
                message=f"Would delete orphan VM '{name}'",
                progress_current=index,
                progress_total=total,
            )
            continue

        try:
            await rest_bulk_delete_async(nb, VIRTUAL_MACHINES_PATH, [record_id])
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
                "Failed to delete orphan VM id=%s name=%s run_id=%s stale_run_id=%s",
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
                message=f"Failed to delete orphan VM '{name}'",
                progress_current=index,
                progress_total=total,
                error=str(error),
            )
            await _emit_summary(
                stream,
                deleted=deleted,
                failed=failed,
                skipped=skipped,
                message=(
                    f"Orphan VM sweep failed after deleting {deleted} of {total} candidate(s)"
                ),
            )
            raise ProxboxException(
                message="Error while sweeping orphan virtual machines.",
                detail=str(error),
            ) from error

        deleted += 1
        logger.info(
            "Deleted orphan VM id=%s name=%s run_id=%s stale_run_id=%s tag_slugs=%s",
            record_id,
            name,
            run_id,
            item_extra.get("stale_run_id"),
            item_extra.get("tag_slugs"),
        )
        await _emit_item_progress(
            stream,
            item=item,
            operation=ItemOperation.DELETED,
            status="completed",
            message=f"Deleted orphan VM '{name}'",
            progress_current=index,
            progress_total=total,
        )

    message = (
        f"Orphan VM sweep dry-run completed: {total} candidate(s), 0 deleted"
        if dry_run
        else f"Orphan VM sweep completed: {deleted} deleted, {skipped} skipped"
    )
    await _emit_summary(
        stream,
        deleted=deleted,
        failed=failed,
        skipped=skipped,
        message=message,
    )
    return {
        "run_id": run_id,
        "dry_run": dry_run,
        "candidates": total,
        "deleted": deleted,
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
            "failed": 0,
            "skipped": 0,
        }

    candidates = await find_orphan_vms(nb, run_id)
    result = await delete_orphan_vms(
        nb,
        candidates,
        run_id=run_id,
        dry_run=dry_run,
        stream=stream,
        touched_vm_ids=touched_vm_ids,
    )
    return {"enabled": enabled, **result}
