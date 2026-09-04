"""Best-effort readers for netbox-proxbox typed sync-state sidecars."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_first_async, rest_list_async, rest_list_paginated_async
from proxbox_api.services.sync.sync_state_writer import (
    VM_SYNC_STATE_PATH,
    _is_sidecar_unavailable,
    _record_id,
    _record_to_dict,
)

VIRTUAL_MACHINES_PATH = "/api/virtualization/virtual-machines/"

_UNAVAILABLE_READER_SIDECAR_PATHS: set[str] = set()


@dataclass(frozen=True, slots=True)
class SyncStateVMResolution:
    """Resolved NetBox VM record and the backing lookup source."""

    record: object
    record_id: int
    source: str


@dataclass(frozen=True, slots=True)
class _VMIdentityCandidate:
    record_id: int
    source: str
    record: object | None = None


@dataclass(frozen=True, slots=True)
class SidecarVMOrphanScan:
    """VM orphan candidates and current rows proven by a sidecar list pass."""

    stale_candidates: list[dict[str, object]]
    current_vm_ids: set[int]
    sidecar_unavailable: bool = False
    sidecar_read_failed: bool = False


@dataclass(frozen=True, slots=True)
class VMSyncStateIdentityScan:
    """VM sync-state rows plus an explicit sidecar-read outcome.

    Callers that require verified identity must distinguish an optional route
    that is unavailable from a transient read failure.  An empty ``rows``
    tuple alone cannot carry that distinction.
    """

    rows: tuple[dict[str, object], ...]
    sidecar_unavailable: bool = False
    sidecar_read_failed: bool = False


@dataclass(frozen=True, slots=True)
class VMRoleSnapshotRead:
    """One role snapshot plus whether absence was positively verified."""

    snapshot_id: int | None
    verified: bool


@dataclass(frozen=True, slots=True)
class VMRoleSnapshotScan:
    """Fleet role snapshots with explicit global and per-VM read uncertainty."""

    values: dict[int, int]
    unverified_vm_ids: frozenset[int] = frozenset()
    read_verified: bool = True

    def for_vm(self, vm_id: int | None) -> VMRoleSnapshotRead:
        if vm_id is None:
            return VMRoleSnapshotRead(snapshot_id=None, verified=False)
        if vm_id in self.values:
            return VMRoleSnapshotRead(snapshot_id=self.values[vm_id], verified=True)
        return VMRoleSnapshotRead(
            snapshot_id=None,
            verified=self.read_verified and vm_id not in self.unverified_vm_ids,
        )


def reset_sidecar_reader_availability_cache() -> None:
    """Clear the current sync-run memo of unavailable optional sidecar read routes."""
    _UNAVAILABLE_READER_SIDECAR_PATHS.clear()


def _as_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(cast("object", value))
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _relation_id_from_field(record: dict[str, object], field: str) -> int | None:
    return _record_id(record.get(field))


def _record_cluster_id(record: object | None) -> int | None:
    data = _record_to_dict(record)
    if data is None:
        return None
    return _relation_id_from_field(data, "cluster")


def _sidecar_text(value: object) -> str:
    return str(value or "").strip()


def _sidecar_is_unavailable(path: str) -> bool:
    return path in _UNAVAILABLE_READER_SIDECAR_PATHS


def _memoize_sidecar_failure(path: str, error: Exception) -> None:
    if _is_sidecar_unavailable(error):
        _UNAVAILABLE_READER_SIDECAR_PATHS.add(path)
        logger.debug(
            "Skipping Proxbox sync-state sidecar read because %s is unavailable: %s",
            path,
            getattr(error, "detail", str(error)),
        )
    else:
        logger.warning(
            "Proxbox sync-state sidecar read failed at %s: %s",
            path,
            getattr(error, "detail", str(error)),
        )


async def _list_sidecars(
    nb: object,
    *,
    query: dict[str, object],
    page_size: int | None = None,
) -> list[object] | None:
    if _sidecar_is_unavailable(VM_SYNC_STATE_PATH):
        return None
    try:
        if page_size is not None:
            return await rest_list_paginated_async(
                nb,
                VM_SYNC_STATE_PATH,
                base_query=query,
                page_size=page_size,
            )
        return await rest_list_async(nb, VM_SYNC_STATE_PATH, query=query)
    except Exception as exc:  # noqa: BLE001 - optional sidecar reads fall back to CF path
        _memoize_sidecar_failure(VM_SYNC_STATE_PATH, exc)
        return None


async def _scan_sidecars(
    nb: object,
    *,
    query: dict[str, object],
    page_size: int | None = None,
) -> tuple[list[object] | None, bool]:
    if _sidecar_is_unavailable(VM_SYNC_STATE_PATH):
        return None, False
    try:
        if page_size is not None:
            return (
                await rest_list_paginated_async(
                    nb,
                    VM_SYNC_STATE_PATH,
                    base_query=query,
                    page_size=page_size,
                ),
                False,
            )
        return await rest_list_async(nb, VM_SYNC_STATE_PATH, query=query), False
    except Exception as exc:  # noqa: BLE001 - distinguish unavailable from transient failure
        unavailable = _is_sidecar_unavailable(exc)
        _memoize_sidecar_failure(VM_SYNC_STATE_PATH, exc)
        return None, not unavailable


async def _first_sidecar(
    nb: object,
    *,
    query: dict[str, object],
) -> object | None:
    rows = await _list_sidecars(nb, query={**query, "limit": 2})
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            "Proxbox sync-state VM sidecar lookup was ambiguous for query=%s; refusing the lookup",
            query,
        )
        return None
    return rows[0]


async def _fetch_vm_by_id(nb: object, vm_id: int) -> object | None:
    try:
        return await rest_first_async(
            nb,
            VIRTUAL_MACHINES_PATH,
            query={"id": vm_id, "limit": 2},
        )
    except Exception as exc:  # noqa: BLE001 - optional sidecar relation lookup
        logger.debug(
            "Failed to fetch NetBox VM id=%s from sidecar relation: %s",
            vm_id,
            exc,
        )
        return None


async def _list_sidecar_vm_identity_candidates(
    nb: object,
    *,
    proxmox_vm_id: int,
    endpoint_id: int | None,
    cluster_id: int | None,
) -> tuple[list[_VMIdentityCandidate] | None, bool]:
    query: dict[str, object] = {"proxmox_vm_id": proxmox_vm_id}
    if endpoint_id is not None:
        query["proxmox_endpoint_raw_id"] = endpoint_id
    sidecar_limit = 50 if cluster_id is not None else 2
    sidecars, sidecar_read_failed = await _scan_sidecars(
        nb,
        query={**query, "limit": sidecar_limit},
    )
    if sidecars is None:
        return None, sidecar_read_failed

    candidates: list[_VMIdentityCandidate] = []
    refused = False
    for sidecar in sidecars:
        data = _record_to_dict(sidecar)
        if data is None:
            continue
        if (
            endpoint_id is not None
            and _as_positive_int(data.get("proxmox_endpoint_raw_id")) != endpoint_id
        ):
            logger.debug(
                "Ignoring VM sidecar row with mismatched proxmox_endpoint_raw_id: "
                "expected=%s row=%s",
                endpoint_id,
                data,
            )
            continue
        vm_id = _relation_id_from_field(data, "virtual_machine")
        if vm_id is None:
            logger.debug("Ignoring VM sidecar row without virtual_machine relation: %s", data)
            continue
        record: object | None = None
        if cluster_id is not None:
            record = await _fetch_vm_by_id(nb, vm_id)
            resolved_cluster_id = _record_cluster_id(record)
            if resolved_cluster_id is None:
                refused = True
                logger.warning(
                    "Refusing VM sidecar match for vmid=%s endpoint_id=%s cluster_id=%s "
                    "because NetBox VM id=%s cluster could not be verified",
                    proxmox_vm_id,
                    endpoint_id,
                    cluster_id,
                    vm_id,
                )
                continue
            if resolved_cluster_id != cluster_id:
                logger.warning(
                    "Rejecting VM sidecar match for vmid=%s endpoint_id=%s: "
                    "NetBox VM id=%s belongs to cluster_id=%s, expected cluster_id=%s",
                    proxmox_vm_id,
                    endpoint_id,
                    vm_id,
                    resolved_cluster_id,
                    cluster_id,
                )
                continue
        candidates.append(_VMIdentityCandidate(record_id=vm_id, source="sidecar", record=record))
    return candidates, refused


async def _resolve_unique_vm_identity_candidate(
    nb: object,
    *,
    proxmox_vm_id: int,
    endpoint_id: int | None = None,
    cluster_id: int | None = None,
) -> tuple[SyncStateVMResolution | None, bool]:
    sidecar_candidates, sidecar_refused = await _list_sidecar_vm_identity_candidates(
        nb,
        proxmox_vm_id=proxmox_vm_id,
        endpoint_id=endpoint_id,
        cluster_id=cluster_id,
    )
    if sidecar_candidates is None:
        logger.warning(
            "Proxbox VM sync-state lookup for vmid=%s endpoint_id=%s cluster_id=%s "
            "could not read the typed sidecar; refusing to treat the VM identity as "
            "verifiably absent",
            proxmox_vm_id,
            endpoint_id,
            cluster_id,
        )
        return None, True
    if sidecar_refused:
        logger.warning(
            "Proxbox VM sync-state lookup for vmid=%s endpoint_id=%s cluster_id=%s "
            "could not verify sidecar identity; refusing to treat the VM as absent",
            proxmox_vm_id,
            endpoint_id,
            cluster_id,
        )
        return None, True

    candidates = sidecar_candidates or []
    candidates_by_id = {candidate.record_id: candidate for candidate in candidates}
    if len(candidates_by_id) > 1:
        logger.warning(
            "Proxbox VM sync-state lookup was ambiguous for vmid=%s endpoint_id=%s "
            "cluster_id=%s; matched NetBox VM ids=%s",
            proxmox_vm_id,
            endpoint_id,
            cluster_id,
            sorted(candidates_by_id),
        )
        return None, True
    if not candidates_by_id:
        return None, False

    record_id = next(iter(candidates_by_id))
    sidecar_candidate = next(
        (candidate for candidate in candidates if candidate.record_id == record_id),
        None,
    )
    hydrated_sidecar_candidate = next(
        (
            candidate
            for candidate in candidates
            if candidate.record_id == record_id and candidate.record is not None
        ),
        None,
    )
    source = sidecar_candidate.source if sidecar_candidate is not None else "sidecar"
    record = (
        hydrated_sidecar_candidate.record
        if hydrated_sidecar_candidate is not None
        else await _fetch_vm_by_id(nb, record_id)
    )
    if record is None:
        return None, False
    return SyncStateVMResolution(record=record, record_id=record_id, source=source), False


async def resolve_virtual_machine_by_sync_state(
    nb: object,
    *,
    proxmox_vm_id: int | str | None,
    endpoint_id: int | None = None,
    cluster_id: int | None = None,
    fail_on_ambiguous: bool = False,
) -> SyncStateVMResolution | None:
    """Resolve a NetBox VM only when the typed sidecar match is unique."""
    vmid = _as_positive_int(proxmox_vm_id)
    if vmid is None:
        return None

    resolution, _ambiguous = await _resolve_unique_vm_identity_candidate(
        nb,
        proxmox_vm_id=vmid,
        endpoint_id=endpoint_id,
        cluster_id=cluster_id,
    )
    if _ambiguous and fail_on_ambiguous:
        raise ProxboxException(
            message="Refusing to create or bind a VM from ambiguous sync-state identity.",
            detail=(
                f"vmid={vmid} endpoint_id={endpoint_id} cluster_id={cluster_id}; "
                "sidecar identity was ambiguous or could not be verified"
            ),
        )
    return resolution


async def resolve_virtual_machine_id_by_sync_state(
    nb: object,
    *,
    proxmox_vm_id: int | str | None,
    endpoint_id: int | None = None,
    cluster_id: int | None = None,
) -> int | None:
    """Resolve only the NetBox VM id when the typed sidecar match is unique."""
    resolution = await resolve_virtual_machine_by_sync_state(
        nb,
        proxmox_vm_id=proxmox_vm_id,
        endpoint_id=endpoint_id,
        cluster_id=cluster_id,
    )
    return resolution.record_id if resolution is not None else None


async def resolve_unique_virtual_machine_by_sync_state(
    nb: object,
    *,
    proxmox_vm_id: int | str | None,
) -> tuple[SyncStateVMResolution | None, bool]:
    """Resolve by VMID only when the sidecar match set is unique."""
    vmid = _as_positive_int(proxmox_vm_id)
    if vmid is None:
        return None, False

    return await _resolve_unique_vm_identity_candidate(nb, proxmox_vm_id=vmid)


async def resolve_vm_sidecar_by_parent_id(nb: object, vm_id: int) -> dict[str, object] | None:
    """Return the VM sidecar row for a NetBox VM id, if the optional API is available."""
    sidecar = await _first_sidecar(nb, query={"virtual_machine_id": vm_id})
    return _record_to_dict(sidecar) if sidecar is not None else None


def _collapse_vm_last_synced_name(parent_id: int, values: set[str]) -> str | None:
    if not values:
        return None
    if len(values) == 1:
        return next(iter(values))
    logger.warning(
        "Omitting proxmox_vm_name evidence for NetBox VM id=%s because "
        "multiple sync-state sidecar rows disagree: %s",
        parent_id,
        sorted(values),
    )
    return None


def _collapse_vm_last_synced_role_id(parent_id: int, values: set[int]) -> int | None:
    if not values:
        return None
    if len(values) == 1:
        return next(iter(values))
    logger.warning(
        "Omitting proxmox_last_synced_role_id evidence for NetBox VM id=%s "
        "because multiple sync-state sidecar rows disagree: %s",
        parent_id,
        sorted(values),
    )
    return None


async def load_vm_last_synced_name(nb: object, vm_id: int) -> str | None:
    """Return one VM's last synced Proxmox name from sidecar evidence.

    This mirrors :func:`load_vm_last_synced_names` for the individual sync path:
    agreeing non-empty duplicate sidecar rows collapse to one value, disagreeing
    non-empty rows are treated as no evidence, and blank/missing values remain
    absent.
    """
    parent_id = _as_positive_int(vm_id)
    if parent_id is None:
        return None
    rows = await _list_sidecars(nb, query={"virtual_machine_id": parent_id})
    if not rows:
        return None

    values: set[str] = set()
    for row in rows:
        sidecar = _record_to_dict(row)
        if not sidecar:
            continue
        row_parent_id = _relation_id_from_field(sidecar, "virtual_machine")
        if row_parent_id is None:
            continue
        if row_parent_id != parent_id:
            logger.debug(
                "Ignoring VM sync-state sidecar row returned for virtual_machine_id=%s "
                "because it belongs to NetBox VM id=%s: %s",
                parent_id,
                row_parent_id,
                sidecar,
            )
            continue
        name = _sidecar_text(sidecar.get("proxmox_vm_name"))
        if name:
            values.add(name)
    return _collapse_vm_last_synced_name(parent_id, values)


async def load_vm_last_synced_names(
    nb: object,
    *,
    page_size: int = 500,
) -> dict[int, str]:
    """Map NetBox VM id -> the Proxmox name recorded at the last successful sync.

    Fetched once per sync pass rather than per VM. The name resolver needs this
    for every VM it examines, and a per-VM lookup would add an N+1 REST round
    trip to a pass that already runs over the whole fleet.

    Returns an empty mapping when the sidecar API is unavailable or the field is
    not populated, which callers must treat as "no evidence" and fall back to
    their previous behaviour -- every row is blank until it has been re-synced
    at least once after the field was introduced.
    """
    rows = await _list_sidecars(nb, query={}, page_size=page_size)
    if not rows:
        return {}

    names_by_parent_id: dict[int, set[str]] = {}
    for row in rows:
        sidecar = _record_to_dict(row)
        if not sidecar:
            continue
        parent_id = _relation_id_from_field(sidecar, "virtual_machine")
        if parent_id is None:
            continue
        name = _sidecar_text(sidecar.get("proxmox_vm_name"))
        if name:
            names_by_parent_id.setdefault(parent_id, set()).add(name)

    names: dict[int, str] = {}
    for parent_id, values in names_by_parent_id.items():
        name = _collapse_vm_last_synced_name(parent_id, values)
        if name is not None:
            names[parent_id] = name
    return names


async def load_vm_last_synced_role_ids(
    nb: object,
    *,
    page_size: int = 500,
) -> dict[int, int]:
    """Compatibility wrapper returning verified typed role snapshots only."""
    return (await scan_vm_last_synced_role_ids(nb, page_size=page_size)).values


async def scan_vm_last_synced_role_ids(
    nb: object,
    *,
    page_size: int = 500,
) -> VMRoleSnapshotScan:
    """Read all typed role snapshots without confusing failures with absence."""
    rows, read_failed = await _scan_sidecars(nb, query={}, page_size=page_size)
    if rows is None:
        return VMRoleSnapshotScan(values={}, read_verified=False)

    values_by_parent_id: dict[int, set[int]] = {}
    for row in rows:
        sidecar = _record_to_dict(row)
        if not sidecar:
            continue
        parent_id = _relation_id_from_field(sidecar, "virtual_machine")
        if parent_id is None:
            continue
        role_id = _as_positive_int(sidecar.get("proxmox_last_synced_role_id"))
        if role_id is not None:
            values_by_parent_id.setdefault(parent_id, set()).add(role_id)

    snapshots: dict[int, int] = {}
    unverified_vm_ids: set[int] = set()
    for parent_id, values in values_by_parent_id.items():
        role_id = _collapse_vm_last_synced_role_id(parent_id, values)
        if role_id is not None:
            snapshots[parent_id] = role_id
        elif values:
            unverified_vm_ids.add(parent_id)
    return VMRoleSnapshotScan(
        values=snapshots,
        unverified_vm_ids=frozenset(unverified_vm_ids),
        read_verified=not read_failed,
    )


async def load_vm_sync_state_identities(
    nb: object,
    *,
    page_size: int = 200,
) -> VMSyncStateIdentityScan:
    """Load the complete VM identity sidecar once with explicit failure state."""

    sidecars, sidecar_read_failed = await _scan_sidecars(
        nb,
        query={},
        page_size=page_size,
    )
    if sidecars is None:
        return VMSyncStateIdentityScan(
            rows=(),
            sidecar_unavailable=not sidecar_read_failed,
            sidecar_read_failed=sidecar_read_failed,
        )

    rows = tuple(row for sidecar in sidecars if (row := _record_to_dict(sidecar)) is not None)
    return VMSyncStateIdentityScan(rows=rows)


async def resolve_vm_last_run_id(
    nb: object,
    *,
    vm_record: dict[str, object] | None,
) -> str | None:
    """Read VM last-run state from the typed sidecar."""
    vm_id = _record_id(vm_record) if vm_record is not None else None
    if vm_id is not None:
        sidecar = await resolve_vm_sidecar_by_parent_id(nb, vm_id)
        if sidecar is not None and "last_run_id" in sidecar:
            value = _sidecar_text(sidecar.get("last_run_id"))
            return value or None
    return None


async def resolve_vm_last_synced_role_id(
    nb: object,
    *,
    vm_record: dict[str, object] | None,
) -> int | None:
    """Compatibility wrapper returning a snapshot only when one is verified."""
    return (
        await read_vm_last_synced_role(
            nb,
            vm_record=vm_record,
        )
    ).snapshot_id


def _typed_vm_role_snapshot(
    vm_id: int,
    rows: list[object],
) -> VMRoleSnapshotRead:
    values: set[int] = set()
    for row in rows:
        sidecar = _record_to_dict(row)
        if sidecar is None:
            continue
        if _relation_id_from_field(sidecar, "virtual_machine") != vm_id:
            continue
        role_id = _as_positive_int(sidecar.get("proxmox_last_synced_role_id"))
        if role_id is not None:
            values.add(role_id)
    if len(values) == 1:
        return VMRoleSnapshotRead(snapshot_id=next(iter(values)), verified=True)
    if len(values) > 1:
        _collapse_vm_last_synced_role_id(vm_id, values)
        return VMRoleSnapshotRead(snapshot_id=None, verified=False)
    return VMRoleSnapshotRead(snapshot_id=None, verified=True)


async def read_vm_last_synced_role(
    nb: object,
    *,
    vm_record: dict[str, object] | None,
) -> VMRoleSnapshotRead:
    """Read a role snapshot while preserving unavailable/ambiguous outcomes."""
    vm_id = _record_id(vm_record) if vm_record is not None else None
    if vm_id is None:
        typed_read = VMRoleSnapshotRead(snapshot_id=None, verified=False)
    else:
        rows, _read_failed = await _scan_sidecars(
            nb,
            query={"virtual_machine_id": vm_id, "limit": 2},
        )
        typed_read = (
            VMRoleSnapshotRead(snapshot_id=None, verified=False)
            if rows is None
            else _typed_vm_role_snapshot(vm_id, rows)
        )
    return typed_read


async def scan_vm_sidecar_orphan_candidates(
    nb: object,
    *,
    run_id: str,
    vm_slugs: Iterable[str],
) -> SidecarVMOrphanScan | None:
    """Return stale VM records and first-pass-current VM ids from sidecars.

    ``last_run_id`` is serialized by netbox-proxbox but is not exposed by the
    VM sync-state filterset, so this deliberately fetches sidecar rows without
    unsupported filters and applies the stale/current decision client-side.
    """
    candidates_by_id: dict[int, dict[str, object]] = {}
    current_vm_ids: set[int] = set()
    sidecars, sidecar_read_failed = await _scan_sidecars(nb, query={}, page_size=200)
    if sidecars is None:
        return SidecarVMOrphanScan(
            stale_candidates=[],
            current_vm_ids=set(),
            sidecar_unavailable=not sidecar_read_failed,
            sidecar_read_failed=sidecar_read_failed,
        )
    for sidecar in sidecars:
        data = _record_to_dict(sidecar)
        if data is None or "last_run_id" not in data:
            continue
        vm_id = _relation_id_from_field(data, "virtual_machine")
        if vm_id is None:
            continue
        if _sidecar_text(data.get("last_run_id")) == run_id:
            current_vm_ids.add(vm_id)
            continue
        if vm_id in candidates_by_id:
            continue
        vm_record = await _fetch_vm_by_id(nb, vm_id)
        vm_data = _record_to_dict(vm_record) if vm_record is not None else None
        if vm_data is None:
            continue
        if not _record_has_any_tag_slug(vm_data, vm_slugs):
            continue
        vm_data["_proxbox_last_run_id"] = data.get("last_run_id")
        vm_data["_proxmox_vm_id"] = data.get("proxmox_vm_id")
        candidates_by_id[vm_id] = vm_data
    return SidecarVMOrphanScan(
        stale_candidates=list(candidates_by_id.values()),
        current_vm_ids=current_vm_ids,
    )


async def list_stale_vm_sidecar_candidates(
    nb: object,
    *,
    run_id: str,
    vm_slugs: Iterable[str],
) -> list[dict[str, object]] | None:
    """Return stale VM records selected from sidecar last_run_id values."""
    scan = await scan_vm_sidecar_orphan_candidates(
        nb,
        run_id=run_id,
        vm_slugs=vm_slugs,
    )
    return scan.stale_candidates if scan is not None else None


def _record_has_any_tag_slug(record: dict[str, object], vm_slugs: Iterable[str]) -> bool:
    wanted = {str(slug).strip() for slug in vm_slugs if str(slug).strip()}
    if not wanted:
        return False
    tags = record.get("tags")
    if not isinstance(tags, list):
        return False
    for tag in tags:
        if isinstance(tag, dict):
            raw = tag.get("slug") or tag.get("name")
        else:
            raw = tag
        if str(raw or "").strip() in wanted:
            return True
    return False
