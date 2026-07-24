"""Virtual-machine task-history synchronization.

Archived task rows are collected once per selected Proxmox node, associated to
NetBox VMs in memory, and reconciled in one bounded NetBox operation.  This is
deliberately different from the live-task API: archive rows already contain
their terminal status and therefore require no per-UPID status requests.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import RestRecord, rest_bulk_reconcile_async, rest_list_async
from proxbox_api.proxmox_to_netbox.models import NetBoxTaskHistorySyncState
from proxbox_api.runtime_settings import get_int
from proxbox_api.services.custom_fields import custom_fields_enabled, warn_legacy_custom_fields
from proxbox_api.services.proxmox_helpers import dump_models, get_node_tasks
from proxbox_api.services.sync._helpers import _extract_fk_id, _normalize_text
from proxbox_api.services.sync.sync_state_reader import load_vm_sync_state_identities
from proxbox_api.services.sync.vm_helpers import (
    list_netbox_virtual_machines_by_ids,
    require_selected_netbox_vm_coverage,
)
from proxbox_api.services.sync.vmid_helpers import (
    extract_proxmox_endpoint_id,
    extract_proxmox_session_endpoint_id,
    extract_proxmox_vm_type,
    extract_proxmox_vmid,
    normalize_positive_int,
)

_TASK_ARCHIVE_PAGE_SIZE = 500
_TASK_HISTORY_PATH = "/api/plugins/proxbox/task-history/"


def _resolve_fetch_concurrency() -> int:
    return get_int(
        settings_key="proxmox_fetch_concurrency",
        env="PROXBOX_PROXMOX_FETCH_CONCURRENCY",
        default=4,
        minimum=1,
    )


_TASK_HISTORY_PATCHABLE_FIELDS = frozenset(
    {
        # Identity corrections are intentional: a historic UPID associated to
        # the wrong guest/type must be able to self-heal.
        "virtual_machine",
        "vm_type",
        "end_time",
        "status",
        "task_state",
        "exitstatus",
        "tags",
        "custom_fields",
    }
)


@dataclass(frozen=True)
class _VMTarget:
    netbox_id: int
    endpoint_id: int | None
    cluster_name: str
    vmid: int
    vm_type: str
    name: str


@dataclass(frozen=True)
class _ArchiveNode:
    session: object
    endpoint_id: int | None
    cluster_name: str
    node_name: str


@dataclass(frozen=True)
class _ArchiveResult:
    source: _ArchiveNode
    tasks: tuple[dict[str, object], ...]
    errors: int
    successful_request: bool


@dataclass(frozen=True)
class _ArchiveSelection:
    nodes: tuple[_ArchiveNode, ...]
    missing_scopes: tuple[tuple[int | None, str], ...]


def _as_mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    serialize = getattr(value, "serialize", None)
    if callable(serialize):
        serialized = serialize()
        if isinstance(serialized, Mapping):
            return {str(key): item for key, item in serialized.items()}
    return {}


def _cluster_name(value: object) -> str:
    mapping = _as_mapping(value)
    cluster = mapping.get("cluster")
    if isinstance(cluster, Mapping):
        cluster_mapping = {str(key): item for key, item in cluster.items()}
        return _normalize_text(cluster_mapping.get("name")) or ""
    return _normalize_text(getattr(cluster, "name", None)) or ""


def _status_name(value: object) -> str:
    if isinstance(value, Mapping):
        mapping = {str(key): item for key, item in value.items()}
        return _normalize_text(mapping.get("name")) or ""
    return _normalize_text(getattr(value, "name", None)) or ""


def _status_nodes(value: object) -> list[str]:
    if isinstance(value, Mapping):
        mapping = {str(key): item for key, item in value.items()}
        raw_nodes_value = mapping.get("node_list")
    else:
        raw_nodes_value = getattr(value, "node_list", None)
    raw_nodes = (
        raw_nodes_value
        if isinstance(raw_nodes_value, Sequence) and not isinstance(raw_nodes_value, (str, bytes))
        else []
    )
    names: list[str] = []
    for node in raw_nodes:
        if isinstance(node, Mapping):
            node_mapping = {str(key): item for key, item in node.items()}
            name = _normalize_text(node_mapping.get("node") or node_mapping.get("name"))
        else:
            name = _normalize_text(getattr(node, "node", None) or getattr(node, "name", None))
        if name:
            names.append(name)
    return names


def _humanize_task_type(task_type: str) -> str:
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
    lowered = task_type.lower()
    for key, value in mapping.items():
        if key in lowered:
            return value
    return task_type.capitalize()


def _format_task_description(vm_type: str, task_id: str | None, task_type: str) -> str:
    vm_display = {"lxc": "CT", "ct": "CT", "qemu": "QEMU"}.get(vm_type.lower(), vm_type.upper())
    action = _humanize_task_type(task_type)
    return f"{vm_display} {task_id} - {action}" if task_id else f"{vm_display} - {action}"


def _epoch_iso(value: object, *, fallback: datetime | None = None) -> str | None:
    if value in (None, ""):
        return fallback.isoformat() if fallback is not None else None
    try:
        return datetime.fromtimestamp(float(str(value)), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return fallback.isoformat() if fallback is not None else None


def _build_task_payload(
    virtual_machine_id: int,
    vm_type: str,
    task: dict[str, object],
    task_status: dict[str, object],
    tag_refs: list[dict[str, object]],
    now: datetime,
) -> dict[str, object]:
    """Build a NetBox payload from an archived PVE task row."""

    upid = _normalize_text(task.get("upid")) or ""
    task_id = _normalize_text(task.get("id") or task.get("vmid"))
    task_type = _normalize_text(task.get("type")) or "unknown"
    final_status = (
        _normalize_text(task_status.get("exitstatus"))
        or _normalize_text(task_status.get("status"))
        or _normalize_text(task.get("exitstatus"))
        or _normalize_text(task.get("status"))
        or "unknown"
    )

    def _optional_int(value: object) -> int | None:
        try:
            return int(str(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "virtual_machine": virtual_machine_id,
        "vm_type": vm_type,
        "upid": upid,
        "node": _normalize_text(task.get("node")) or "",
        "pid": _optional_int(task.get("pid")),
        "pstart": _optional_int(task.get("pstart")),
        "task_id": task_id,
        "task_type": task_type,
        "username": _normalize_text(task.get("user")) or "root@pam",
        "start_time": _epoch_iso(task.get("starttime"), fallback=now),
        "end_time": _epoch_iso(task_status.get("endtime") or task.get("endtime")),
        "description": _format_task_description(vm_type, task_id, task_type),
        "status": final_status,
        "task_state": "stopped",
        "exitstatus": final_status,
        "tags": tag_refs,
        "custom_fields": {},
    }


def _current_task_history(record: dict[str, object]) -> dict[str, object]:
    return {
        "virtual_machine": _extract_fk_id(record.get("virtual_machine")),
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
    }


async def _list_all_vms_with_proxmox_id(
    nb: object,
    batch_size: int = 500,
    *,
    netbox_vm_ids: list[int] | None = None,
) -> list[RestRecord | dict[str, object]]:
    """Read the VM table; bound explicit multi-ID filters to small requests."""

    if netbox_vm_ids is not None and not netbox_vm_ids:
        return []
    if netbox_vm_ids is None:
        return await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"limit": batch_size},
        )

    return await list_netbox_virtual_machines_by_ids(nb, netbox_vm_ids)


def _vm_target(vm: object) -> _VMTarget | None:
    mapping = _as_mapping(vm)
    netbox_id = normalize_positive_int(mapping.get("id"))
    vmid = normalize_positive_int(extract_proxmox_vmid(mapping))
    cluster_name = _cluster_name(mapping)
    if netbox_id is None or vmid is None or not cluster_name:
        return None
    return _VMTarget(
        netbox_id=netbox_id,
        endpoint_id=extract_proxmox_endpoint_id(mapping),
        cluster_name=cluster_name,
        vmid=vmid,
        vm_type=extract_proxmox_vm_type(mapping) or "qemu",
        name=_normalize_text(mapping.get("name")) or str(netbox_id),
    )


def _sidecar_vm_target(
    vm: object,
    sidecar: Mapping[str, object],
) -> _VMTarget | None:
    """Build a target from one authoritative VM sync-state sidecar row."""

    mapping = _as_mapping(vm)
    netbox_id = normalize_positive_int(mapping.get("id"))
    sidecar_parent_id = normalize_positive_int(_extract_fk_id(sidecar.get("virtual_machine")))
    vmid = normalize_positive_int(sidecar.get("proxmox_vm_id"))
    endpoint_id = normalize_positive_int(sidecar.get("proxmox_endpoint_raw_id"))
    cluster_name = _normalize_text(sidecar.get("proxmox_cluster_name")) or ""
    vm_type = extract_proxmox_vm_type(dict(sidecar))
    if (
        netbox_id is None
        or sidecar_parent_id != netbox_id
        or vmid is None
        or endpoint_id is None
        or not cluster_name
        or vm_type is None
    ):
        return None
    return _VMTarget(
        netbox_id=netbox_id,
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        vmid=vmid,
        vm_type=vm_type,
        name=_normalize_text(mapping.get("name")) or str(netbox_id),
    )


def _resolve_vm_target_from_sources(
    vm: object,
    sidecar_rows: Sequence[Mapping[str, object]],
    *,
    legacy_fallback_allowed: bool,
) -> tuple[_VMTarget | None, int | None, str]:
    """Resolve one VM target and describe which identity branch was used."""

    mapping = _as_mapping(vm)
    netbox_id = normalize_positive_int(mapping.get("id"))
    if netbox_id is None:
        return None, None, "invalid_vm"
    if sidecar_rows:
        if len(sidecar_rows) != 1:
            return None, netbox_id, "invalid_sidecar"
        target = _sidecar_vm_target(vm, sidecar_rows[0])
        return (
            (target, netbox_id, "sidecar")
            if target is not None
            else (None, netbox_id, "invalid_sidecar")
        )
    if not legacy_fallback_allowed:
        return None, netbox_id, "unresolved"
    target = _vm_target(vm)
    return (target, netbox_id, "legacy") if target is not None else (None, netbox_id, "unresolved")


async def _resolve_vm_targets(  # noqa: C901
    nb: object,
    vms: Sequence[object],
    *,
    explicitly_selected: bool,
) -> tuple[list[_VMTarget], int]:
    """Resolve owned VM identity while ignoring unmanaged rows only estate-wide."""

    if not vms:
        return [], 0
    identity_scan = await load_vm_sync_state_identities(nb)
    legacy_fallback_allowed = custom_fields_enabled()
    if (
        identity_scan.sidecar_unavailable or identity_scan.sidecar_read_failed
    ) and not legacy_fallback_allowed:
        reason = "temporarily failed" if identity_scan.sidecar_read_failed else "is unavailable"
        raise ProxboxException(
            message="Unable to verify VM identity for task-history sync",
            detail=(
                f"The VM sync-state sidecar {reason} and legacy custom-field fallback is disabled."
            ),
            http_status_code=502,
        )

    relevant_vm_ids = {
        vm_id
        for vm in vms
        if (vm_id := normalize_positive_int(_as_mapping(vm).get("id"))) is not None
    }
    sidecars_by_vm_id: dict[int, list[dict[str, object]]] = {}
    invalid_sidecar_refs: set[str] = set()
    for sidecar in identity_scan.rows:
        parent_id = normalize_positive_int(_extract_fk_id(sidecar.get("virtual_machine")))
        if parent_id is None:
            # An estate-wide run owns the complete VM set and therefore treats
            # an unparented sidecar as corrupt. A selected run cannot safely
            # attribute it to its scope, so the selected VM will instead fail
            # below if its own identity is absent.
            if not explicitly_selected:
                sidecar_id = normalize_positive_int(sidecar.get("id"))
                invalid_sidecar_refs.add(
                    f"sidecar:{sidecar_id}" if sidecar_id is not None else "sidecar:unknown"
                )
            continue
        if parent_id not in relevant_vm_ids:
            continue
        sidecars_by_vm_id.setdefault(parent_id, []).append(sidecar)
        if (
            normalize_positive_int(sidecar.get("proxmox_vm_id")) is None
            or normalize_positive_int(sidecar.get("proxmox_endpoint_raw_id")) is None
            or not (_normalize_text(sidecar.get("proxmox_cluster_name")) or "")
            or extract_proxmox_vm_type(dict(sidecar)) is None
        ):
            invalid_sidecar_refs.add(str(parent_id))

    for parent_id, rows in sidecars_by_vm_id.items():
        if len(rows) != 1:
            invalid_sidecar_refs.add(str(parent_id))

    targets: list[_VMTarget] = []
    skipped = 0
    unresolved_ids: list[int] = []
    used_legacy_fallback = False
    for vm in vms:
        mapping = _as_mapping(vm)
        netbox_id = normalize_positive_int(mapping.get("id"))
        sidecar_rows = sidecars_by_vm_id.get(netbox_id, []) if netbox_id is not None else []
        target, resolved_id, source = _resolve_vm_target_from_sources(
            vm,
            sidecar_rows,
            legacy_fallback_allowed=legacy_fallback_allowed,
        )
        if target is not None:
            targets.append(target)
            used_legacy_fallback = used_legacy_fallback or source == "legacy"
        elif source == "invalid_sidecar" and resolved_id is not None:
            invalid_sidecar_refs.add(str(resolved_id))
            skipped += 1
        elif resolved_id is not None:
            unresolved_ids.append(resolved_id)
            skipped += 1
        else:
            skipped += 1

    if invalid_sidecar_refs:
        ordered_refs = sorted(invalid_sidecar_refs)
        sample = ", ".join(ordered_refs[:10])
        suffix = "..." if len(ordered_refs) > 10 else ""
        raise ProxboxException(
            message="Unable to verify VM identity for task-history sync",
            detail=(
                "VM sync-state identity is malformed, incomplete, or duplicated for "
                f"relevant NetBox VM/sidecar id(s): {sample}{suffix}. Refusing legacy custom-field "
                "fallback because a sidecar row is present."
            ),
            http_status_code=502,
        )

    if unresolved_ids and explicitly_selected:
        sample = ", ".join(str(vm_id) for vm_id in unresolved_ids[:10])
        suffix = "..." if len(unresolved_ids) > 10 else ""
        raise ProxboxException(
            message="Unable to verify VM identity for task-history sync",
            detail=(
                "VM sync-state identity is missing, incomplete, or ambiguous for "
                f"explicitly selected NetBox VM id(s): {sample}{suffix}."
            ),
            http_status_code=502,
        )

    if used_legacy_fallback:
        warn_legacy_custom_fields("task-history VM identity fallback")
    return targets, skipped


def _selected_archive_nodes(
    pxs: Sequence[object] | None,
    cluster_status: Sequence[object] | None,
    targets: list[_VMTarget],
) -> _ArchiveSelection:
    exact_scopes = {
        (target.endpoint_id, target.cluster_name)
        for target in targets
        if target.endpoint_id is not None
    }
    legacy_clusters = {target.cluster_name for target in targets if target.endpoint_id is None}
    requested_scopes = exact_scopes | {(None, cluster_name) for cluster_name in legacy_clusters}
    covered_scopes: set[tuple[int | None, str]] = set()
    selected: list[_ArchiveNode] = []
    seen: set[tuple[int, str, str]] = set()
    for session, status in zip(pxs or [], cluster_status or []):
        cluster_name = _status_name(status)
        endpoint_id = extract_proxmox_session_endpoint_id(session)
        matched_scopes: set[tuple[int | None, str]] = set()
        exact_scope = (endpoint_id, cluster_name)
        if exact_scope in exact_scopes:
            matched_scopes.add(exact_scope)
        legacy_scope = (None, cluster_name)
        if cluster_name in legacy_clusters:
            matched_scopes.add(legacy_scope)
        if not matched_scopes:
            continue
        node_names = _status_nodes(status)
        if not node_names:
            continue
        covered_scopes.update(matched_scopes)
        for node_name in node_names:
            signature = (id(session), cluster_name, node_name)
            if signature in seen:
                continue
            seen.add(signature)
            selected.append(
                _ArchiveNode(
                    session=session,
                    endpoint_id=endpoint_id,
                    cluster_name=cluster_name,
                    node_name=node_name,
                )
            )
    missing_scopes = tuple(
        sorted(
            requested_scopes - covered_scopes,
            key=lambda scope: (scope[1], scope[0] if scope[0] is not None else -1),
        )
    )
    return _ArchiveSelection(nodes=tuple(selected), missing_scopes=missing_scopes)


def _page_signature(tasks: list[dict[str, object]]) -> tuple[str, ...]:
    return tuple(
        _normalize_text(task.get("upid"))
        or repr(sorted((str(key), repr(value)) for key, value in task.items()))
        for task in tasks
    )


def _append_unique_archive_tasks(
    tasks: list[dict[str, object]],
    *,
    node_name: str,
    seen_upids: set[str],
    collected: list[dict[str, object]],
) -> int:
    added = 0
    for raw_task in tasks:
        task = dict(raw_task)
        task.setdefault("node", node_name)
        upid = _normalize_text(task.get("upid"))
        if not upid or upid in seen_upids:
            continue
        seen_upids.add(upid)
        collected.append(task)
        added += 1
    return added


async def _fetch_node_archive(
    source: _ArchiveNode,
    *,
    semaphore: asyncio.Semaphore,
    until: int,
    vmid: int | None = None,
) -> _ArchiveResult:
    collected: list[dict[str, object]] = []
    seen_upids: set[str] = set()
    seen_pages: set[tuple[str, ...]] = set()
    offset = 0
    errors = 0
    successful_request = False

    while True:
        try:
            async with semaphore:
                raw = get_node_tasks(
                    source.session,
                    node=source.node_name,
                    vmid=vmid,
                    source="archive",
                    start=offset,
                    limit=_TASK_ARCHIVE_PAGE_SIZE,
                    until=until,
                )
                if inspect.isawaitable(raw):
                    raw = await raw
            tasks = dump_models(raw)
            successful_request = True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            errors += 1
            logger.warning(
                "Task archive collection failed for cluster=%s node=%s offset=%s; "
                "retaining %s earlier row(s): %s",
                source.cluster_name,
                source.node_name,
                offset,
                len(collected),
                error,
            )
            break

        if not tasks:
            break
        signature = _page_signature(tasks)
        if signature in seen_pages:
            errors += 1
            logger.warning(
                "Task archive repeated a page for cluster=%s node=%s offset=%s; stopping",
                source.cluster_name,
                source.node_name,
                offset,
            )
            break
        seen_pages.add(signature)

        added = _append_unique_archive_tasks(
            tasks,
            node_name=source.node_name,
            seen_upids=seen_upids,
            collected=collected,
        )
        if added == 0:
            errors += 1
            logger.warning(
                "Task archive produced no new UPIDs for cluster=%s node=%s offset=%s; stopping",
                source.cluster_name,
                source.node_name,
                offset,
            )
            break
        if len(tasks) < _TASK_ARCHIVE_PAGE_SIZE:
            break
        offset += _TASK_ARCHIVE_PAGE_SIZE

    return _ArchiveResult(
        source=source,
        tasks=tuple(collected),
        errors=errors,
        successful_request=successful_request,
    )


async def _reconcile_task_payloads(
    nb: object,
    payloads: list[dict[str, object]],
    *,
    base_query: dict[str, object] | None,
) -> int:
    if not payloads:
        return 0
    result = await rest_bulk_reconcile_async(
        nb,
        _TASK_HISTORY_PATH,
        payloads=payloads,
        lookup_fields=["upid"],
        schema=NetBoxTaskHistorySyncState,
        current_normalizer=_current_task_history,
        patchable_fields=_TASK_HISTORY_PATCHABLE_FIELDS,
        base_query=base_query,
        fallback_to_individual=False,
    )
    return result.created + result.updated + result.unchanged


async def _emit_task_history_start(
    websocket: object | None,
    *,
    use_websocket: bool,
    targets: list[_VMTarget],
) -> None:
    if websocket is None:
        return
    emit_discovery = getattr(websocket, "emit_discovery", None)
    if callable(emit_discovery):
        emitted = emit_discovery(
            phase="task-history",
            items=[{"name": target.name, "type": "vm"} for target in targets],
            message=f"Starting task history sync for {len(targets)} VMs",
        )
        if inspect.isawaitable(emitted):
            await emitted
        return
    send_json = getattr(websocket, "send_json", None)
    if use_websocket and callable(send_json):
        sent = send_json(
            {
                "object": "task_history",
                "type": "sync",
                "data": {"status": "started", "message": "Starting task history sync"},
            }
        )
        if inspect.isawaitable(sent):
            await sent


async def _emit_task_history_summary(
    websocket: object | None,
    *,
    use_websocket: bool,
    reconciled: int,
    skipped: int,
    errors: int = 0,
) -> None:
    if websocket is None:
        return
    emit_summary = getattr(websocket, "emit_phase_summary", None)
    if callable(emit_summary):
        emitted = emit_summary(
            phase="task-history",
            created=reconciled,
            failed=errors,
            skipped=skipped,
            message=(
                f"Task history sync {'completed with degraded coverage' if errors else 'completed'}: "
                f"{reconciled} records reconciled, {skipped} skipped, {errors} fetch error(s)"
            ),
        )
        if inspect.isawaitable(emitted):
            await emitted
        return
    send_json = getattr(websocket, "send_json", None)
    if use_websocket and callable(send_json):
        sent = send_json(
            {
                "object": "task_history",
                "end": True,
                "data": {
                    "status": "warning" if errors else "completed",
                    "errors": errors,
                },
            }
        )
        if inspect.isawaitable(sent):
            await sent


async def sync_all_virtual_machine_task_histories(  # noqa: C901
    netbox_session: object,
    pxs: Sequence[object] | None,
    cluster_status: Sequence[object] | None,
    tag_refs: list[dict[str, object]] | None = None,
    websocket: object | None = None,
    use_websocket: bool = False,
    fetch_max_concurrency: int | None = None,
    netbox_vm_ids: list[int] | None = None,
) -> dict[str, object]:
    """Collect each selected node archive once and globally reconcile tasks."""

    if fetch_max_concurrency is not None and fetch_max_concurrency < 1:
        raise ProxboxException(
            message="Invalid task-history fetch concurrency",
            detail="fetch_max_concurrency must be at least 1 when provided.",
            http_status_code=422,
        )

    try:
        vms = await _list_all_vms_with_proxmox_id(
            netbox_session,
            netbox_vm_ids=netbox_vm_ids,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error("Unable to list VMs for task-history sync: %s", error)
        raise ProxboxException(
            message="Unable to list VMs for task-history sync",
            detail=str(error),
            http_status_code=502,
        ) from error

    if netbox_vm_ids is not None:
        vms = require_selected_netbox_vm_coverage(
            vms,
            netbox_vm_ids,
            operation="task-history sync",
        )

    targets, skipped = await _resolve_vm_targets(
        netbox_session,
        vms,
        explicitly_selected=netbox_vm_ids is not None,
    )
    if not targets:
        return {"count": 0, "created": 0, "skipped": skipped}

    selection = _selected_archive_nodes(pxs, cluster_status, targets)
    nodes = list(selection.nodes)
    for endpoint_id, cluster_name in selection.missing_scopes:
        logger.warning(
            "Task-history target scope has no discovered non-empty node coverage: "
            "endpoint=%s cluster=%s",
            endpoint_id if endpoint_id is not None else "legacy",
            cluster_name,
        )
    if not nodes:
        message = "Task-history archive has no selected Proxmox nodes"
        missing = ", ".join(
            f"endpoint={endpoint_id if endpoint_id is not None else 'legacy'}/cluster={cluster_name}"
            for endpoint_id, cluster_name in selection.missing_scopes
        )
        raise ProxboxException(
            message=message,
            detail=f"Missing target scope coverage: {missing}" if missing else None,
            http_status_code=502,
        )

    await _emit_task_history_start(
        websocket,
        use_websocket=use_websocket,
        targets=targets,
    )

    exact: dict[tuple[int, str, int], list[_VMTarget]] = {}
    legacy: dict[tuple[str, int], list[_VMTarget]] = {}
    for target in targets:
        if target.endpoint_id is None:
            legacy.setdefault((target.cluster_name, target.vmid), []).append(target)
        else:
            exact.setdefault((target.endpoint_id, target.cluster_name, target.vmid), []).append(
                target
            )
    exact_cluster_vmids = {(cluster_name, vmid) for _endpoint_id, cluster_name, vmid in exact}
    mixed_identity_collisions = exact_cluster_vmids.intersection(legacy)
    cluster_source_scopes: dict[str, set[tuple[str, int]]] = {}
    for node in nodes:
        scope = (
            ("endpoint", node.endpoint_id)
            if node.endpoint_id is not None
            else ("session", id(node.session))
        )
        cluster_source_scopes.setdefault(node.cluster_name, set()).add(scope)
    legacy_safe_clusters = {
        name for name, scopes in cluster_source_scopes.items() if len(scopes) == 1
    }

    until = int(datetime.now(timezone.utc).timestamp())
    semaphore = asyncio.Semaphore(
        fetch_max_concurrency if fetch_max_concurrency is not None else _resolve_fetch_concurrency()
    )
    archive_results = await asyncio.gather(
        *(_fetch_node_archive(node, semaphore=semaphore, until=until) for node in nodes),
        return_exceptions=True,
    )

    successful_nodes = 0
    errors = len(selection.missing_scopes)
    observations: dict[str, list[tuple[_VMTarget, dict[str, object]]]] = {}
    for archive_result in archive_results:
        if isinstance(archive_result, asyncio.CancelledError):
            raise archive_result
        if isinstance(archive_result, BaseException):
            errors += 1
            logger.warning("Unexpected task archive collection failure: %s", archive_result)
            continue
        errors += archive_result.errors
        if archive_result.successful_request:
            successful_nodes += 1
        for task in archive_result.tasks:
            task_vmid = normalize_positive_int(task.get("id") or task.get("vmid"))
            upid = _normalize_text(task.get("upid"))
            if task_vmid is None or not upid:
                skipped += 1
                continue
            if (archive_result.source.cluster_name, task_vmid) in mixed_identity_collisions:
                errors += 1
                skipped += 1
                logger.warning(
                    "Skipping task UPID %s because an endpoint-scoped VM and an "
                    "endpoint-less legacy VM both claim cluster=%s vmid=%s",
                    upid,
                    archive_result.source.cluster_name,
                    task_vmid,
                )
                continue
            candidates: list[_VMTarget] = []
            ownership_ambiguous = False
            if archive_result.source.endpoint_id is not None:
                candidates = exact.get(
                    (
                        archive_result.source.endpoint_id,
                        archive_result.source.cluster_name,
                        task_vmid,
                    ),
                    [],
                )
                ownership_ambiguous = len(candidates) > 1
            if not candidates:
                legacy_candidates = legacy.get(
                    (archive_result.source.cluster_name, task_vmid),
                    [],
                )
                if legacy_candidates:
                    if archive_result.source.cluster_name in legacy_safe_clusters:
                        candidates = legacy_candidates
                        ownership_ambiguous = len(candidates) > 1
                    else:
                        ownership_ambiguous = True
            if ownership_ambiguous:
                errors += 1
                skipped += 1
                logger.warning(
                    "Skipping task UPID %s because VM ownership is ambiguous for "
                    "endpoint=%s cluster=%s vmid=%s",
                    upid,
                    archive_result.source.endpoint_id,
                    archive_result.source.cluster_name,
                    task_vmid,
                )
                continue
            if len(candidates) != 1:
                skipped += 1
                continue
            observations.setdefault(upid, []).append((candidates[0], task))

    if successful_nodes == 0:
        message = "Task-history archive collection failed for every selected node"
        raise ProxboxException(
            message=message,
            detail=f"{errors or len(nodes)} node archive request(s) failed",
            http_status_code=502,
        )

    normalized_tags = [tag for tag in (tag_refs or []) if tag.get("name") and tag.get("slug")]
    now = datetime.now(timezone.utc)
    payloads: list[dict[str, object]] = []
    for upid, upid_observations in observations.items():
        owners = {observation[0].netbox_id for observation in upid_observations}
        if len(owners) != 1:
            errors += 1
            skipped += 1
            logger.warning(
                "Skipping task UPID %s because it maps to multiple VM owners: %s",
                upid,
                sorted(owners),
            )
            continue
        # The same UPID may legitimately appear on old and new nodes after a
        # migration. Keep the most complete/latest archive row for that owner.
        target, task = max(
            upid_observations,
            key=lambda observation: (
                normalize_positive_int(observation[1].get("endtime")) or 0,
                normalize_positive_int(observation[1].get("starttime")) or 0,
            ),
        )
        payloads.append(
            _build_task_payload(
                virtual_machine_id=target.netbox_id,
                vm_type=target.vm_type,
                task=task,
                task_status=task,
                tag_refs=normalized_tags,
                now=now,
            )
        )

    try:
        reconciled = await _reconcile_task_payloads(
            netbox_session,
            payloads,
            # NetBox uses UPID as the task-history lookup key, and a row may
            # already be attached to the wrong VM. A VM filter would hide that
            # row and turn the repair into a duplicate create, so every
            # aggregate scans this table once.
            base_query=None,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error("Bulk task-history reconciliation failed: %s", error)
        raise ProxboxException(
            message="Bulk task-history reconciliation failed",
            detail=str(error),
            http_status_code=502,
        ) from error

    result: dict[str, object] = {
        "count": len(targets),
        "created": reconciled,
        "skipped": skipped,
    }
    if errors:
        result.update({"degraded": True, "errors": errors})

    await _emit_task_history_summary(
        websocket,
        use_websocket=use_websocket,
        reconciled=reconciled,
        skipped=skipped,
        errors=errors,
    )
    return result


def _find_cluster_sources(
    pxs: Sequence[object] | None,
    cluster_status: Sequence[object] | None,
    cluster_name: str | None,
    endpoint_id: int | None = None,
) -> list[_ArchiveNode]:
    matches = [
        (session, status)
        for session, status in zip(pxs or [], cluster_status or [])
        if not cluster_name or _status_name(status) == cluster_name
    ]
    if endpoint_id is not None:
        matches = [
            (session, status)
            for session, status in matches
            if extract_proxmox_session_endpoint_id(session) == endpoint_id
        ]
    elif len(matches) != 1:
        logger.warning(
            "Refusing ambiguous targeted task-history lookup for cluster %s across %s session(s)",
            cluster_name or "<unspecified>",
            len(matches),
        )
        return []

    sources: list[_ArchiveNode] = []
    for session, status in matches:
        status_name = _status_name(status)
        for node_name in _status_nodes(status):
            sources.append(
                _ArchiveNode(
                    session=session,
                    endpoint_id=extract_proxmox_session_endpoint_id(session),
                    cluster_name=status_name,
                    node_name=node_name,
                )
            )
    return sources


async def sync_virtual_machine_task_history(
    *,
    netbox_session: object,
    pxs: Sequence[object] | None,
    cluster_status: Sequence[object] | None,
    virtual_machine_id: int,
    proxmox_vmid: int,
    vm_type: str,
    cluster_name: str | None,
    proxmox_endpoint_id: int | None = None,
    tag_refs: list[dict[str, object]] | None = None,
    websocket: object | None = None,
    use_websocket: bool = False,
    fetch_max_concurrency: int | None = None,
) -> int:
    """Compatibility entry point for a deliberately targeted one-VM sync."""

    del websocket, use_websocket
    sources = _find_cluster_sources(
        pxs,
        cluster_status,
        cluster_name,
        endpoint_id=proxmox_endpoint_id,
    )
    if not sources:
        logger.warning(
            "No Proxmox nodes found for cluster %s while syncing task history",
            cluster_name,
        )
        return 0

    semaphore = asyncio.Semaphore(fetch_max_concurrency or _resolve_fetch_concurrency())
    until = int(datetime.now(timezone.utc).timestamp())
    archive_results = await asyncio.gather(
        *(
            _fetch_node_archive(
                source,
                semaphore=semaphore,
                until=until,
                vmid=proxmox_vmid,
            )
            for source in sources
        ),
        return_exceptions=True,
    )
    tasks_by_upid: dict[str, dict[str, object]] = {}
    for archive_result in archive_results:
        if isinstance(archive_result, asyncio.CancelledError):
            raise archive_result
        if isinstance(archive_result, BaseException):
            logger.warning("Task archive collection failed: %s", archive_result)
            continue
        for task in archive_result.tasks:
            task_vmid = normalize_positive_int(task.get("id") or task.get("vmid"))
            upid = _normalize_text(task.get("upid"))
            if task_vmid == proxmox_vmid and upid:
                tasks_by_upid.setdefault(upid, task)

    normalized_tags = [tag for tag in (tag_refs or []) if tag.get("name") and tag.get("slug")]
    now = datetime.now(timezone.utc)
    payloads = [
        _build_task_payload(
            virtual_machine_id=virtual_machine_id,
            vm_type=vm_type if vm_type in {"qemu", "lxc"} else "qemu",
            task=task,
            task_status=task,
            tag_refs=normalized_tags,
            now=now,
        )
        for task in tasks_by_upid.values()
    ]
    try:
        return await _reconcile_task_payloads(
            netbox_session,
            payloads,
            base_query=None,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error(
            "Bulk task-history reconciliation failed for VM %s: %s",
            virtual_machine_id,
            error,
        )
        return 0
