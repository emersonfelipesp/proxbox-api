"""VM resource filtering utilities - extracted from sync_vm.py."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.services.sync.sync_state_reader import load_vm_sync_state_identities
from proxbox_api.services.sync.vm_helpers import (
    list_netbox_virtual_machines_by_ids,
    parse_proxmox_net_configs,
    relation_id,
    relation_name,
    to_mapping,
)
from proxbox_api.services.sync.vmid_helpers import (
    extract_proxmox_endpoint_id,
    extract_proxmox_session_endpoint_id,
    extract_proxmox_vm_type,
    extract_proxmox_vmid,
    normalize_positive_int,
)


class SelectionMode(str, Enum):
    """How a selection reacts to a VM whose Proxmox ownership cannot be resolved.

    ``STRICT`` fails the whole selection closed on the first such VM. Routes that
    address one VM by path use it: there is no other VM to make progress on, and
    a wrong owner must never be guessed. ``LENIENT`` drops the VM with a warning
    and lets the remaining VMs proceed. Staged and estate-wide runs use it so a
    single incomplete or ambiguous record cannot abort every other VM.
    """

    STRICT = "strict"
    LENIENT = "lenient"


SelectionSkip = dict[str, object]
"""One dropped VM: ``{"netbox_vm_id": int, "reason": str}``."""


class SelectionResult(list):
    """Selected VMs (or filtered cluster resources) plus the VMs dropped from them.

    A plain ``list`` subclass so existing callers keep working unchanged;
    ``skipped`` is only populated by ``SelectionMode.LENIENT``.
    """

    def __init__(
        self,
        values: list | None = None,
        *,
        skipped: list[SelectionSkip] | None = None,
    ) -> None:
        super().__init__(values or [])
        self.skipped: list[SelectionSkip] = skipped if skipped is not None else []


def selection_skips(result: object) -> list[SelectionSkip]:
    """Return the skips recorded on a selection result, or none for a plain list."""

    skipped = getattr(result, "skipped", None)
    return list(skipped) if isinstance(skipped, list) else []


def skip_or_raise(
    error: ProxboxException,
    *,
    mode: SelectionMode,
    netbox_vm_id: object,
    skipped: list[SelectionSkip] | None,
) -> None:
    """Re-raise ``error`` in strict mode; otherwise log and record the skip."""

    if mode is SelectionMode.STRICT:
        raise error
    reason = str(error.detail or error.message)
    logger.warning(
        "Skipping NetBox VM id %s during lenient VM selection: %s",
        netbox_vm_id,
        reason,
    )
    if skipped is not None:
        skipped.append({"netbox_vm_id": netbox_vm_id, "reason": reason})


class _OwnerMatch(NamedTuple):
    """One live Proxmox resource matched to a selected owner."""

    position: tuple[int, object]
    resource: dict[str, object]
    netbox_id: int


@dataclass(frozen=True, slots=True)
class _SelectedVMOwner:
    """Collision-safe owner identity for one explicitly selected NetBox VM."""

    netbox_id: int
    endpoint_id: int
    cluster_name: str
    vmid: int
    vm_type: str

    @property
    def resource_key(self) -> tuple[int, str, int, str]:
        return (self.endpoint_id, self.cluster_name, self.vmid, self.vm_type)


def _normalize_cluster_name(value: object) -> str:
    return str(value or "").strip().casefold()


def _field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _source_endpoint_ids_by_cluster(
    pxs: object,
    cluster_status: object,
) -> dict[str, set[int | None]]:
    """Index every observed cluster alias by its available endpoint owner."""

    sessions = list(pxs or [])
    statuses = list(cluster_status or [])
    endpoint_ids_by_cluster: dict[str, set[int | None]] = {}
    for index, session in enumerate(sessions):
        endpoint_id = extract_proxmox_session_endpoint_id(session)
        status = statuses[index] if index < len(statuses) else None
        for raw_name in (
            _field(status, "name"),
            _field(session, "name"),
            _field(session, "cluster_name"),
        ):
            cluster_name = _normalize_cluster_name(raw_name)
            if cluster_name:
                endpoint_ids_by_cluster.setdefault(cluster_name, set()).add(endpoint_id)
    return endpoint_ids_by_cluster


def _selection_error(detail: str) -> ProxboxException:
    return ProxboxException(
        message="Unable to resolve explicitly selected VM ownership",
        detail=detail,
        http_status_code=502,
    )


def _requested_ids(netbox_vm_ids: list[int]) -> list[int]:
    requested: list[int] = []
    seen: set[int] = set()
    for raw_id in netbox_vm_ids:
        vm_id = normalize_positive_int(raw_id)
        if vm_id is None:
            raise _selection_error(f"Invalid selected NetBox VM id: {raw_id!r}.")
        if vm_id not in seen:
            seen.add(vm_id)
            requested.append(vm_id)
    return requested


def _selected_sidecar_cluster_name(sidecar: dict[str, object]) -> str:
    return _normalize_cluster_name(
        sidecar.get("proxmox_cluster_name") or relation_name(sidecar.get("proxmox_cluster"))
    )


def _overlay_selected_sidecar_identity(
    vm: dict[str, object],
    sidecar: dict[str, object],
    *,
    netbox_id: int,
) -> dict[str, object]:
    """Overlay one verified typed sidecar identity onto a selected VM copy."""

    sidecar_cluster_name = _selected_sidecar_cluster_name(sidecar)
    endpoint_id = normalize_positive_int(sidecar.get("proxmox_endpoint_raw_id"))
    vmid = normalize_positive_int(extract_proxmox_vmid(sidecar))
    vm_type = extract_proxmox_vm_type(sidecar)
    if not sidecar_cluster_name or endpoint_id is None or vmid is None or vm_type is None:
        raise _selection_error(
            f"NetBox VM id {netbox_id} has incomplete or conflicting typed "
            "Proxbox sync-state ownership; endpoint, cluster, positive "
            "VMID, and VM type are required."
        )

    hydrated = dict(vm)
    hydrated.update(
        {
            "proxmox_endpoint_id": endpoint_id,
            "proxmox_vm_id": vmid,
            "proxmox_vm_type": vm_type,
        }
    )
    cluster = vm.get("cluster")
    cluster_id = relation_id(cluster)
    hydrated["cluster"] = {
        **({"id": cluster_id} if cluster_id is not None else {}),
        "name": str(sidecar.get("proxmox_cluster_name") or sidecar_cluster_name).strip(),
    }
    return hydrated


def _hydrate_selected_vm_identity(
    vm: dict[str, object],
    *,
    sidecars_by_vm_id: dict[int, list[dict[str, object]]],
) -> dict[str, object]:
    netbox_id = relation_id(vm.get("id"))
    if netbox_id is None:
        return vm
    candidates = sidecars_by_vm_id.get(netbox_id, [])
    if len(candidates) > 1:
        raise _selection_error(
            f"NetBox VM id {netbox_id} requires exactly one complete typed "
            f"Proxbox sync-state owner; found {len(candidates)}."
        )
    if candidates:
        return _overlay_selected_sidecar_identity(
            vm,
            candidates[0],
            netbox_id=netbox_id,
        )
    raise _selection_error(f"NetBox VM id {netbox_id} has no typed Proxbox sync-state owner.")


def _group_sidecars_by_vm_id(
    rows: Iterable[dict[str, object]],
    selected_ids: set[int],
) -> dict[int, list[dict[str, object]]]:
    sidecars_by_vm_id: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        parent_id = relation_id(row.get("virtual_machine"))
        if parent_id in selected_ids:
            sidecars_by_vm_id.setdefault(parent_id, []).append(row)
    return sidecars_by_vm_id


def _hydrate_selection(
    vms: list[dict[str, object]],
    *,
    sidecars_by_vm_id: dict[int, list[dict[str, object]]],
    require_all: bool,
    mode: SelectionMode,
    skipped: list[SelectionSkip] | None,
) -> list[dict[str, object]]:
    """Overlay each VM's sidecar identity, dropping unresolvable VMs when lenient.

    A VM with no sidecar at all is unmanaged and silently left out unless
    ``require_all`` says it was explicitly selected. Only a sidecar that is
    present but incomplete or duplicated, or an explicitly selected VM without
    one, is a warned skip.
    """

    hydrated: list[dict[str, object]] = []
    for vm in vms:
        netbox_id = relation_id(vm.get("id"))
        if not require_all and not sidecars_by_vm_id.get(netbox_id):
            continue
        try:
            hydrated.append(_hydrate_selected_vm_identity(vm, sidecars_by_vm_id=sidecars_by_vm_id))
        except ProxboxException as error:
            skip_or_raise(error, mode=mode, netbox_vm_id=netbox_id, skipped=skipped)
    return hydrated


def _hydrated_owner_key(vm: dict[str, object]) -> tuple[int, str, int, str] | None:
    endpoint_id = normalize_positive_int(vm.get("proxmox_endpoint_id"))
    vmid = normalize_positive_int(vm.get("proxmox_vm_id"))
    cluster_name = _normalize_cluster_name(relation_name(vm.get("cluster")))
    vm_type = str(vm.get("proxmox_vm_type") or "")
    if endpoint_id is None or vmid is None or not cluster_name or not vm_type:
        return None
    return (endpoint_id, cluster_name, vmid, vm_type)


def _sidecar_owner_key(row: dict[str, object]) -> tuple[int, str, int, str] | None:
    """Return the normalized Proxmox owner key of one complete sidecar row."""

    endpoint_id = normalize_positive_int(row.get("proxmox_endpoint_raw_id"))
    vmid = normalize_positive_int(extract_proxmox_vmid(row))
    cluster_name = _selected_sidecar_cluster_name(row)
    vm_type = extract_proxmox_vm_type(row)
    if endpoint_id is None or vmid is None or not cluster_name or not vm_type:
        return None
    return (endpoint_id, cluster_name, vmid, str(vm_type))


def _owner_claimants_index(
    rows: Iterable[dict[str, object]],
) -> dict[tuple[int, str, int, str], list[int]]:
    """Index every NetBox VM claiming each owner across the whole sidecar scan.

    The index deliberately covers rows of VMs that are not selected, so a
    selected VM is recognised as sharing its owner with an unselected one.
    Rows with an incomplete identity cannot name an owner and are ignored.
    """

    claimants: dict[tuple[int, str, int, str], set[int]] = {}
    for row in rows:
        parent_id = relation_id(row.get("virtual_machine"))
        key = _sidecar_owner_key(row)
        if parent_id is not None and key is not None:
            claimants.setdefault(key, set()).add(parent_id)
    return {key: sorted(ids) for key, ids in claimants.items()}


def _reject_shared_hydrated_owners(
    hydrated: list[dict[str, object]],
    *,
    claimants_by_owner: dict[tuple[int, str, int, str], list[int]],
    mode: SelectionMode,
    skipped: list[SelectionSkip] | None,
) -> list[dict[str, object]]:
    """Drop every selected VM whose Proxmox owner is claimed by several NetBox VMs.

    Each sidecar is valid on its own, but two NetBox VMs naming the same
    endpoint/cluster/VMID/type would make later stages write one guest's data
    under either NetBox VM. Which claimant is right cannot be known, so lenient
    mode drops every selected claimant and strict mode raises before any stage
    writes. Claimants outside the selection count as claimants but are not
    themselves processed or reported.
    """

    shared_ids: set[int] = set()
    for vm in hydrated:
        key = _hydrated_owner_key(vm)
        netbox_id = relation_id(vm.get("id"))
        claimant_ids = claimants_by_owner.get(key, []) if key is not None else []
        if key is None or netbox_id is None or len(claimant_ids) < 2:
            continue
        error = _selection_error(
            f"NetBox VM ids {_join_ids(claimant_ids)} claim the same Proxmox "
            f"endpoint/cluster/VMID/type owner (endpoint {key[0]}, cluster "
            f"{key[1]!r}, VMID {key[2]}, type {key[3]})."
        )
        skip_or_raise(error, mode=mode, netbox_vm_id=netbox_id, skipped=skipped)
        shared_ids.add(netbox_id)
    return [vm for vm in hydrated if relation_id(vm.get("id")) not in shared_ids]


def _hydrate_scanned_selection(
    vms: list[dict[str, object]],
    *,
    scan_rows: list[dict[str, object]],
    selected_ids: set[int],
    require_all: bool,
    mode: SelectionMode,
    skipped: list[SelectionSkip] | None,
) -> list[dict[str, object]]:
    """Hydrate a selection from a full sidecar scan and reject shared owners.

    Every entry point that joins a selection to the scan goes through here, so
    the owner-claimant index always covers the whole scan, including rows of
    VMs outside the selection, and none of them can skip the shared-owner check.
    """

    hydrated = _hydrate_selection(
        vms,
        sidecars_by_vm_id=_group_sidecars_by_vm_id(scan_rows, selected_ids),
        require_all=require_all,
        mode=mode,
        skipped=skipped,
    )
    return _reject_shared_hydrated_owners(
        hydrated,
        claimants_by_owner=_owner_claimants_index(scan_rows),
        mode=mode,
        skipped=skipped,
    )


async def _hydrate_selected_sidecar_identities(
    netbox_session: NetBoxSessionDep,
    vms: list[dict[str, object]],
    *,
    mode: SelectionMode = SelectionMode.STRICT,
    skipped: list[SelectionSkip] | None = None,
) -> list[dict[str, object]]:
    """Join explicit selections to authoritative sidecars in one scan."""

    selected_ids = {vm_id for vm in vms if (vm_id := relation_id(vm.get("id"))) is not None}
    if not selected_ids:
        return vms

    scan = await load_vm_sync_state_identities(netbox_session)
    if scan.sidecar_read_failed or scan.sidecar_unavailable:
        outcome = "failed" if scan.sidecar_read_failed else "is unavailable"
        raise _selection_error(
            "Typed Proxbox VM sync-state lookup "
            f"{outcome} while resolving selected NetBox VM id(s): "
            + ", ".join(str(vm_id) for vm_id in sorted(selected_ids))
            + "."
        )

    return _hydrate_scanned_selection(
        vms,
        scan_rows=scan.rows,
        selected_ids=selected_ids,
        require_all=True,
        mode=mode,
        skipped=skipped,
    )


async def hydrate_vm_identities_from_sidecars(
    netbox_session: NetBoxSessionDep,
    vms: list[dict[str, object]],
    *,
    require_all: bool,
    mode: SelectionMode = SelectionMode.STRICT,
) -> SelectionResult:
    """Overlay typed ownership state and optionally skip unmanaged VMs.

    A failed or unavailable sidecar scan is fatal in every mode: it is not a
    per-VM ownership problem, and continuing would treat unreadable state as
    absent. ``mode`` only governs VMs whose own sidecar is unusable; the dropped
    VMs are reported on the result's ``skipped`` attribute.
    """
    selected_ids = {vm_id for vm in vms if (vm_id := relation_id(vm.get("id"))) is not None}
    if not selected_ids:
        return SelectionResult([] if not require_all else vms)

    scan = await load_vm_sync_state_identities(netbox_session)
    if scan.sidecar_read_failed or scan.sidecar_unavailable:
        outcome = "failed" if scan.sidecar_read_failed else "is unavailable"
        raise _selection_error(f"Typed Proxbox VM sync-state lookup {outcome}.")

    skipped: list[SelectionSkip] = []
    hydrated = _hydrate_scanned_selection(
        vms,
        scan_rows=scan.rows,
        selected_ids=selected_ids,
        require_all=require_all,
        mode=mode,
        skipped=skipped,
    )
    return SelectionResult(hydrated, skipped=skipped)


async def hydrate_selected_vm_identities(
    netbox_session: NetBoxSessionDep,
    vms: list[dict[str, object]],
    *,
    mode: SelectionMode = SelectionMode.STRICT,
) -> SelectionResult:
    """Overlay authoritative sidecar identity onto explicitly selected VMs.

    Shared by selection paths outside this module (for example targeted backup
    sync) so every explicit selection resolves ownership sidecar-first. Strict
    mode keeps the malformed/duplicate fail-closed semantics; lenient mode drops
    the offending VM and records it on the result's ``skipped`` attribute.
    """

    return await hydrate_vm_identities_from_sidecars(
        netbox_session,
        vms,
        require_all=True,
        mode=mode,
    )


def _resolve_selected_owner(
    vm: dict[str, object],
    *,
    netbox_id: int,
    endpoint_ids_by_cluster: dict[str, set[int | None]],
    require_stored_endpoint: bool = False,
) -> _SelectedVMOwner:
    vmid = normalize_positive_int(extract_proxmox_vmid(vm))
    vm_type = extract_proxmox_vm_type(vm)
    cluster_name = _normalize_cluster_name(relation_name(vm.get("cluster")))
    if vmid is None or vm_type is None or not cluster_name:
        raise _selection_error(
            f"NetBox VM id {netbox_id} has incomplete Proxmox ownership; "
            "cluster, positive VMID, and VM type are required, and endpoint identity "
            "must be stored or uniquely inferable."
        )

    source_endpoint_ids = endpoint_ids_by_cluster.get(cluster_name, set())
    if not source_endpoint_ids:
        raise _selection_error(
            f"No available Proxmox source owns cluster {cluster_name!r} "
            f"for NetBox VM id {netbox_id}."
        )
    if len(source_endpoint_ids) != 1 or None in source_endpoint_ids:
        displayed_endpoint_ids = sorted(
            "unknown" if endpoint_id is None else str(endpoint_id)
            for endpoint_id in source_endpoint_ids
        )
        raise _selection_error(
            f"Cluster {cluster_name!r} for NetBox VM id {netbox_id} is ambiguous "
            f"across endpoint ids {displayed_endpoint_ids}."
        )

    source_endpoint_id = next(iter(source_endpoint_ids))
    assert source_endpoint_id is not None
    endpoint_id = extract_proxmox_endpoint_id(vm)
    if endpoint_id is None:
        if require_stored_endpoint:
            raise _selection_error(
                f"NetBox VM id {netbox_id} has no stored Proxmox endpoint id; "
                "targeted sync requires endpoint, cluster, VMID, and VM type ownership."
            )
        # Compatibility for records created before endpoint identity was
        # stored. The cluster may supply the owner only when exactly one
        # available endpoint can possibly own it.
        endpoint_id = source_endpoint_id
    elif endpoint_id != source_endpoint_id:
        raise _selection_error(
            f"NetBox VM id {netbox_id} claims endpoint id {endpoint_id}, but "
            f"cluster {cluster_name!r} is owned by endpoint id {source_endpoint_id}."
        )

    return _SelectedVMOwner(
        netbox_id=netbox_id,
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        vmid=vmid,
        vm_type=vm_type,
    )


def _records_by_selected_id(vms: list[dict[str, object]]) -> dict[int, dict[str, object]]:
    return {vm_id: vm for vm in vms if (vm_id := relation_id(vm.get("id"))) is not None}


def _require_returned_selection(
    vms: list[dict[str, object]],
    requested_ids: list[int],
) -> dict[int, dict[str, object]]:
    """Require NetBox to have returned every selected VM, whatever the mode.

    A selection NetBox cannot fully return is a lookup-coverage failure, not a
    per-VM ownership problem, so it stays fatal for lenient runs as well.
    """

    records_by_id = _records_by_selected_id(vms)
    missing_ids = sorted(set(requested_ids).difference(records_by_id))
    if missing_ids:
        raise _selection_error(
            "NetBox did not return explicitly selected VM id(s): "
            + ", ".join(str(vm_id) for vm_id in missing_ids)
            + "."
        )
    return records_by_id


def _join_ids(vm_ids: list[int]) -> str:
    if len(vm_ids) == 2:
        return f"{vm_ids[0]} and {vm_ids[1]}"
    return ", ".join(str(vm_id) for vm_id in vm_ids)


def _reject_conflicting_owners(
    owners: list[_SelectedVMOwner],
    *,
    mode: SelectionMode,
    skipped: list[SelectionSkip] | None,
) -> list[_SelectedVMOwner]:
    """Drop every owner whose endpoint/cluster/VMID/type is claimed more than once.

    Which of several claimants is right cannot be known, so lenient mode drops
    all of them rather than keeping whichever happened to come first.
    """

    ids_by_resource_key: dict[tuple[int, str, int, str], list[int]] = {}
    for owner in owners:
        ids_by_resource_key.setdefault(owner.resource_key, []).append(owner.netbox_id)

    conflicting_ids: set[int] = set()
    for claimant_ids in ids_by_resource_key.values():
        if len(claimant_ids) < 2:
            continue
        error = _selection_error(
            f"NetBox VM ids {_join_ids(claimant_ids)} claim the same "
            "Proxmox endpoint/cluster/VMID/type owner."
        )
        for claimant_id in claimant_ids:
            skip_or_raise(error, mode=mode, netbox_vm_id=claimant_id, skipped=skipped)
            conflicting_ids.add(claimant_id)
    return [owner for owner in owners if owner.netbox_id not in conflicting_ids]


def _resolve_selected_owners(
    vms: list[dict[str, object]],
    *,
    requested_ids: list[int],
    endpoint_ids_by_cluster: dict[str, set[int | None]],
    require_stored_endpoint: bool = False,
    mode: SelectionMode = SelectionMode.STRICT,
    skipped: list[SelectionSkip] | None = None,
) -> list[_SelectedVMOwner]:
    records_by_id = _require_returned_selection(vms, requested_ids)

    owners: list[_SelectedVMOwner] = []
    for netbox_id in requested_ids:
        try:
            owners.append(
                _resolve_selected_owner(
                    records_by_id[netbox_id],
                    netbox_id=netbox_id,
                    endpoint_ids_by_cluster=endpoint_ids_by_cluster,
                    require_stored_endpoint=require_stored_endpoint,
                )
            )
        except ProxboxException as error:
            skip_or_raise(error, mode=mode, netbox_vm_id=netbox_id, skipped=skipped)
    return _reject_conflicting_owners(owners, mode=mode, skipped=skipped)


def _cluster_endpoint_id(
    cluster_key: object,
    endpoint_ids_by_cluster: dict[str, set[int | None]],
) -> int | None:
    """Return the one endpoint id that unambiguously owns ``cluster_key``, if any."""

    source_endpoint_ids = endpoint_ids_by_cluster.get(_normalize_cluster_name(cluster_key), set())
    if len(source_endpoint_ids) != 1 or None in source_endpoint_ids:
        return None
    return next(iter(source_endpoint_ids))


def _match_owner_resources(
    cluster_resources: list[dict[str, object]],
    *,
    owners_by_resource_key: dict[tuple[int, str, int, str], _SelectedVMOwner],
    endpoint_ids_by_cluster: dict[str, set[int | None]],
) -> list[_OwnerMatch]:
    """Pair every live resource with the selected owner it belongs to, in order."""

    matches: list[_OwnerMatch] = []
    for index, cluster in enumerate(cluster_resources):
        if not isinstance(cluster, dict):
            continue
        for cluster_key, resources in cluster.items():
            endpoint_id = _cluster_endpoint_id(cluster_key, endpoint_ids_by_cluster)
            if not isinstance(resources, list) or endpoint_id is None:
                continue
            normalized_cluster = _normalize_cluster_name(cluster_key)
            for resource in resources:
                owner = _owner_for_resource(
                    resource, endpoint_id, normalized_cluster, owners_by_resource_key
                )
                if owner is not None:
                    matches.append(_OwnerMatch((index, cluster_key), resource, owner.netbox_id))
    return matches


def _owner_for_resource(
    resource: object,
    endpoint_id: int,
    normalized_cluster: str,
    owners_by_resource_key: dict[tuple[int, str, int, str], _SelectedVMOwner],
) -> _SelectedVMOwner | None:
    if not isinstance(resource, dict):
        return None
    vm_type = str(resource.get("type") or "").strip().lower()
    vmid = normalize_positive_int(resource.get("vmid"))
    if vm_type not in ("qemu", "lxc") or vmid is None:
        return None
    return owners_by_resource_key.get((endpoint_id, normalized_cluster, vmid, vm_type))


def _reject_selection_gaps(
    vm_ids: list[int],
    *,
    prefix: str,
    mode: SelectionMode,
    skipped: list[SelectionSkip] | None,
) -> set[int]:
    """Fail closed on every id in ``vm_ids`` (strict) or drop each one (lenient)."""

    if not vm_ids:
        return set()
    if mode is SelectionMode.STRICT:
        raise _selection_error(prefix + ", ".join(str(vm_id) for vm_id in vm_ids) + ".")
    for vm_id in vm_ids:
        error = _selection_error(f"{prefix}{vm_id}.")
        skip_or_raise(error, mode=mode, netbox_vm_id=vm_id, skipped=skipped)
    return set(vm_ids)


def _group_owner_matches(
    cluster_resources: list[dict[str, object]],
    matches: list[_OwnerMatch],
    excluded_ids: set[int],
) -> list[dict[str, object]]:
    """Keep only the matched resources, preserving one row per input source row.

    Downstream stages pair ``filtered[i]`` with ``pxs[i]`` and ``cluster_status[i]``
    by position. The invariant is therefore that the result has exactly as many
    rows as ``cluster_resources`` and every row keeps its source index and cluster
    keys; filtering only empties resource lists, it never drops or shifts a row.
    Dropping a row would rebind a later source's resources to an earlier endpoint.
    """

    kept: dict[tuple[int, object], list[dict[str, object]]] = {}
    for match in matches:
        if match.netbox_id not in excluded_ids:
            kept.setdefault(match.position, []).append(match.resource)
    return [
        {key: kept.get((index, key), []) for key in row} if isinstance(row, dict) else {}
        for index, row in enumerate(cluster_resources)
    ]


def _filter_cluster_resources_by_owners(
    cluster_resources: list[dict[str, object]],
    *,
    owners: list[_SelectedVMOwner],
    endpoint_ids_by_cluster: dict[str, set[int | None]],
    mode: SelectionMode = SelectionMode.STRICT,
    skipped: list[SelectionSkip] | None = None,
) -> list[dict[str, object]]:
    """Return exactly one live resource for every resolved selected owner.

    Strict mode fails closed unless each owner matches exactly one live resource.
    Lenient mode drops an owner with no match (for example a guest deleted in
    Proxmox but still in NetBox) or several matches, together with every resource
    it matched, and returns the rest.
    """

    matches = _match_owner_resources(
        cluster_resources,
        owners_by_resource_key={owner.resource_key: owner for owner in owners},
        endpoint_ids_by_cluster=endpoint_ids_by_cluster,
    )
    match_counts = Counter(match.netbox_id for match in matches)
    ambiguous_ids = sorted(owner.netbox_id for owner in owners if match_counts[owner.netbox_id] > 1)
    unresolved_ids = sorted(
        owner.netbox_id for owner in owners if not match_counts[owner.netbox_id]
    )

    excluded_ids = _reject_selection_gaps(
        ambiguous_ids,
        prefix="Multiple live Proxmox resources matched explicitly selected NetBox VM id(s): ",
        mode=mode,
        skipped=skipped,
    )
    excluded_ids |= _reject_selection_gaps(
        unresolved_ids,
        prefix="No exact live Proxmox resource matched explicitly selected NetBox VM id(s): ",
        mode=mode,
        skipped=skipped,
    )
    return _group_owner_matches(cluster_resources, matches, excluded_ids)


async def filter_cluster_resources_for_selected_vm(
    vm_record: object,
    cluster_resources: list[dict[str, object]],
    *,
    netbox_session: NetBoxSessionDep,
    netbox_vm_id: int,
    pxs: object,
    cluster_status: object,
) -> list[dict[str, object]]:
    """Filter a targeted sync by its complete, stored ownership identity.

    Unlike batch compatibility mode, a targeted route may not infer a missing
    endpoint from the cluster and never falls back to a VM name. The selected
    NetBox record must own exactly one live endpoint/cluster/VMID/type tuple.
    """

    requested_ids = _requested_ids([netbox_vm_id])
    vm = to_mapping(vm_record)
    hydrated_vms = await _hydrate_selected_sidecar_identities(
        netbox_session,
        [vm],
    )
    endpoint_ids_by_cluster = _source_endpoint_ids_by_cluster(pxs, cluster_status)
    owners = _resolve_selected_owners(
        hydrated_vms,
        requested_ids=requested_ids,
        endpoint_ids_by_cluster=endpoint_ids_by_cluster,
        require_stored_endpoint=True,
    )
    return _filter_cluster_resources_by_owners(
        cluster_resources,
        owners=owners,
        endpoint_ids_by_cluster=endpoint_ids_by_cluster,
    )


async def filter_cluster_resources_by_netbox_vm_ids(
    netbox_session: NetBoxSessionDep,
    cluster_resources: list[dict[str, object]],
    netbox_vm_ids: list[int],
    *,
    pxs: object,
    cluster_status: object,
    mode: SelectionMode = SelectionMode.STRICT,
) -> SelectionResult:
    """Filter resources by exact selected NetBox VM ownership.

    Args:
        netbox_session: NetBox session
        cluster_resources: List of cluster resources
        netbox_vm_ids: NetBox VM IDs to filter by
        pxs: Available Proxmox sessions carrying endpoint identity
        cluster_status: Cluster status rows aligned with ``pxs``
        mode: ``STRICT`` (default) raises on the first selected VM whose ownership
            cannot be resolved. ``LENIENT`` drops such a VM, logs a warning naming
            it, and returns the resources of the others.

    Returns:
        Filtered cluster resources; the VMs dropped in lenient mode are listed on
        the result's ``skipped`` attribute. A selection NetBox cannot fully
        return, or an unreadable sidecar scan, is fatal in every mode.
    """
    if not netbox_vm_ids:
        return SelectionResult()

    requested_ids = _requested_ids(netbox_vm_ids)
    vms = await list_netbox_virtual_machines_by_ids(netbox_session, requested_ids)
    _require_returned_selection(vms, requested_ids)

    skipped: list[SelectionSkip] = []
    vms = await _hydrate_selected_sidecar_identities(
        netbox_session,
        vms,
        mode=mode,
        skipped=skipped,
    )
    hydrated_ids = set(_records_by_selected_id(vms))
    endpoint_ids_by_cluster = _source_endpoint_ids_by_cluster(pxs, cluster_status)
    owners = _resolve_selected_owners(
        vms,
        requested_ids=[vm_id for vm_id in requested_ids if vm_id in hydrated_ids],
        endpoint_ids_by_cluster=endpoint_ids_by_cluster,
        mode=mode,
        skipped=skipped,
    )
    filtered = _filter_cluster_resources_by_owners(
        cluster_resources,
        owners=owners,
        endpoint_ids_by_cluster=endpoint_ids_by_cluster,
        mode=mode,
        skipped=skipped,
    )
    return SelectionResult(filtered, skipped=skipped)


def parse_network_config(vm_config: dict[str, object]) -> list[dict[str, dict[str, str]]]:
    """Parse Proxmox VM network configuration into list of network dicts.

    Extracts exact net<N> entries from config and parses key=value pairs.

    Args:
        vm_config: VM configuration dict from Proxmox

    Returns:
        List of parsed network configs
    """
    return parse_proxmox_net_configs(vm_config)


def get_interface_name_from_config_and_agent(
    config_interface_name: str,
    config_dict: dict[str, object],
    guest_agent_interfaces: list[dict[str, object]],
    use_guest_agent_name: bool = True,
    vm_interface_sync_strategy: object = "guest_os_model",
) -> str:
    """Determine final interface name from config and guest agent data.

    The current default keeps the Proxmox config name for the core
    virtualization.VMInterface. The deprecated ``legacy_rename`` strategy
    preserves the old behavior and prefers guest-agent names when enabled.

    Args:
        config_interface_name: Interface name from Proxmox config
        config_dict: Network config dictionary
        guest_agent_interfaces: List of interfaces from guest agent
        use_guest_agent_name: Whether to use guest agent names
        vm_interface_sync_strategy: guest_os_model (default) or legacy_rename

    Returns:
        Resolved interface name
    """
    from proxbox_api.services.sync.guest_vm_interface import (
        should_use_guest_agent_core_interface_name,
    )
    from proxbox_api.services.sync.vm_helpers import (
        build_guest_mac_index,
        merged_guest_iface_from_mac_index,
    )

    if not should_use_guest_agent_core_interface_name(
        use_guest_agent_name,
        vm_interface_sync_strategy,
    ):
        return config_interface_name

    # Try to match by MAC address first
    interface_mac = config_dict.get("virtio") or config_dict.get("hwaddr")
    if interface_mac:
        guest_iface = merged_guest_iface_from_mac_index(
            build_guest_mac_index(guest_agent_interfaces),
            interface_mac,
        )
        if guest_iface:
            guest_name = str(guest_iface.get("name") or "").strip()
            if guest_name:
                return guest_name

    # Try to match by name
    for guest_iface in guest_agent_interfaces:
        if str(guest_iface.get("name", "")).strip().lower() == config_interface_name.lower():
            guest_name = str(guest_iface.get("name") or "").strip()
            if guest_name:
                return guest_name

    return config_interface_name
