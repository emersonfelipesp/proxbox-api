"""Strict versus lenient selection of VMs whose Proxmox ownership is unusable.

Staged and estate runs must not let one VM with an incomplete, duplicated or
unresolvable ownership record abort the stage for every other VM. Routes that
address a single VM by path stay fail-closed.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.services.sync.vm_filter import (
    SelectionMode,
    filter_cluster_resources_by_netbox_vm_ids,
    filter_cluster_resources_for_selected_vm,
    hydrate_selected_vm_identities,
    hydrate_vm_identities_from_sidecars,
    selection_skips,
)

GOOD = 1
INCOMPLETE = 2
DUPLICATE = 3
NO_SIDECAR = 4
UNRESOLVABLE_CLUSTER = 5
NOT_LIVE = 6


@pytest.fixture
def proxbox_caplog(caplog: pytest.LogCaptureFixture):
    """Capture the non-propagating application logger."""

    proxbox_logger = logging.getLogger("proxbox")
    proxbox_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="proxbox"):
            yield caplog
    finally:
        proxbox_logger.removeHandler(caplog.handler)


def _vm(netbox_id: int, cluster_name: str = "cluster-a") -> dict[str, object]:
    return {
        "id": netbox_id,
        "name": f"vm-{netbox_id}",
        "cluster": {"id": netbox_id + 1000, "name": cluster_name},
    }


def _sidecar(
    netbox_id: int,
    *,
    cluster_name: str = "cluster-a",
    vmid: int | None = None,
    endpoint_id: int | None = 11,
    vm_type: str = "qemu",
) -> dict[str, object]:
    row: dict[str, object] = {
        "virtual_machine": {"id": netbox_id},
        "proxmox_cluster_name": cluster_name,
        "proxmox_vm_id": vmid if vmid is not None else 100 + netbox_id,
        "proxmox_vm_type": vm_type,
    }
    if endpoint_id is not None:
        row["proxmox_endpoint_raw_id"] = endpoint_id
    return row


def _patch_netbox(monkeypatch, vms: list[dict[str, object]], sidecars: list[dict[str, object]]):
    async def _list(_nb, _path, *, query=None):
        requested = {int(vm_id) for vm_id in (query or {}).get("id", [])}
        return [vm for vm in vms if vm["id"] in requested]

    async def _scan(_nb):
        return SimpleNamespace(
            rows=tuple(sidecars),
            sidecar_unavailable=False,
            sidecar_read_failed=False,
        )

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_filter.load_vm_sync_state_identities",
        _scan,
    )


def _sources(*clusters: tuple[str, int]) -> tuple[list[object], list[object]]:
    pxs = [
        SimpleNamespace(name=name, cluster_name=name, db_endpoint_id=endpoint_id)
        for name, endpoint_id in clusters
    ]
    statuses = [SimpleNamespace(name=name, mode="cluster") for name, _ in clusters]
    return pxs, statuses


def _mixed_estate(monkeypatch) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """One good VM plus one VM per way ownership can be unusable."""

    vms = [
        _vm(GOOD),
        _vm(INCOMPLETE),
        _vm(DUPLICATE),
        _vm(NO_SIDECAR),
        _vm(UNRESOLVABLE_CLUSTER, "ghost"),
        _vm(NOT_LIVE),
    ]
    sidecars = [
        _sidecar(GOOD),
        _sidecar(INCOMPLETE, endpoint_id=None),
        _sidecar(DUPLICATE),
        _sidecar(DUPLICATE, endpoint_id=22),
        _sidecar(UNRESOLVABLE_CLUSTER, cluster_name="ghost"),
        _sidecar(NOT_LIVE, vmid=999),
    ]
    _patch_netbox(monkeypatch, vms, sidecars)
    resources = [
        {"cluster-a": [{"type": "qemu", "vmid": 100 + GOOD, "name": "vm-1"}]},
    ]
    return vms, resources


def _run_filter(resources, ids, mode=SelectionMode.STRICT):
    pxs, statuses = _sources(("cluster-a", 11))
    return asyncio.run(
        filter_cluster_resources_by_netbox_vm_ids(
            object(),
            resources,
            ids,
            pxs=pxs,
            cluster_status=statuses,
            mode=mode,
        )
    )


def test_filter_lenient_keeps_good_vm_and_reports_every_skip(monkeypatch, proxbox_caplog):
    _, resources = _mixed_estate(monkeypatch)
    ids = [GOOD, INCOMPLETE, DUPLICATE, NO_SIDECAR, UNRESOLVABLE_CLUSTER, NOT_LIVE]

    result = _run_filter(resources, ids, SelectionMode.LENIENT)

    assert list(result) == [{"cluster-a": [{"type": "qemu", "vmid": 101, "name": "vm-1"}]}]
    skipped = selection_skips(result)
    assert sorted(skip["netbox_vm_id"] for skip in skipped) == [
        INCOMPLETE,
        DUPLICATE,
        NO_SIDECAR,
        UNRESOLVABLE_CLUSTER,
        NOT_LIVE,
    ]
    assert all(isinstance(skip["reason"], str) and skip["reason"] for skip in skipped)
    reasons = {skip["netbox_vm_id"]: skip["reason"] for skip in skipped}
    assert "incomplete or conflicting" in reasons[INCOMPLETE]
    assert "found 2" in reasons[DUPLICATE]
    assert "no typed Proxbox sync-state owner" in reasons[NO_SIDECAR]
    assert "No available Proxmox source owns cluster 'ghost'" in reasons[UNRESOLVABLE_CLUSTER]
    assert "No exact live Proxmox resource matched" in reasons[NOT_LIVE]
    # Every skip is also logged, naming the NetBox VM.
    for vm_id in (INCOMPLETE, DUPLICATE, NO_SIDECAR, UNRESOLVABLE_CLUSTER, NOT_LIVE):
        assert any(
            f"NetBox VM id {vm_id}" in record.getMessage() and record.levelno == logging.WARNING
            for record in proxbox_caplog.records
        ), vm_id


def test_filter_strict_raises_on_first_bad_vm(monkeypatch):
    _, resources = _mixed_estate(monkeypatch)

    with pytest.raises(ProxboxException, match="selected VM ownership") as exc_info:
        _run_filter(resources, [GOOD, INCOMPLETE, DUPLICATE], SelectionMode.STRICT)

    assert exc_info.value.http_status_code == 502
    assert f"NetBox VM id {INCOMPLETE}" in str(exc_info.value.detail)


def test_filter_defaults_to_strict(monkeypatch):
    _, resources = _mixed_estate(monkeypatch)
    pxs, statuses = _sources(("cluster-a", 11))

    with pytest.raises(ProxboxException, match="selected VM ownership"):
        asyncio.run(
            filter_cluster_resources_by_netbox_vm_ids(
                object(),
                resources,
                [GOOD, INCOMPLETE],
                pxs=pxs,
                cluster_status=statuses,
            )
        )


def test_filter_lenient_all_bad_returns_empty_with_warnings(monkeypatch):
    _, resources = _mixed_estate(monkeypatch)

    result = _run_filter(resources, [INCOMPLETE, DUPLICATE, NOT_LIVE], SelectionMode.LENIENT)

    # Source rows stay aligned with the sources; only their resources are dropped.
    assert list(result) == [{"cluster-a": []}]
    assert sorted(skip["netbox_vm_id"] for skip in selection_skips(result)) == [
        INCOMPLETE,
        DUPLICATE,
        NOT_LIVE,
    ]


def test_filter_strict_raises_when_no_live_resource_matches(monkeypatch):
    _patch_netbox(monkeypatch, [_vm(NOT_LIVE)], [_sidecar(NOT_LIVE, vmid=999)])

    with pytest.raises(ProxboxException, match="selected VM ownership") as exc_info:
        _run_filter([{"cluster-a": [{"type": "qemu", "vmid": 1}]}], [NOT_LIVE])

    assert "No exact live Proxmox resource matched" in str(exc_info.value.detail)


def test_filter_lenient_drops_every_claimant_of_a_shared_owner(monkeypatch):
    # Two NetBox VMs claim the same endpoint/cluster/VMID/type; which is right is
    # unknowable, so neither may be synchronized. A third VM is unaffected.
    _patch_netbox(
        monkeypatch,
        [_vm(1), _vm(2), _vm(3)],
        [_sidecar(1, vmid=500), _sidecar(2, vmid=500), _sidecar(3, vmid=503)],
    )
    resources = [
        {
            "cluster-a": [
                {"type": "qemu", "vmid": 500, "name": "shared"},
                {"type": "qemu", "vmid": 503, "name": "other"},
            ]
        }
    ]

    result = _run_filter(resources, [1, 2, 3], SelectionMode.LENIENT)

    assert list(result) == [{"cluster-a": [{"type": "qemu", "vmid": 503, "name": "other"}]}]
    assert sorted(skip["netbox_vm_id"] for skip in selection_skips(result)) == [1, 2]

    with pytest.raises(ProxboxException, match="selected VM ownership") as exc_info:
        _run_filter(resources, [1, 2, 3], SelectionMode.STRICT)
    assert "NetBox VM ids 1 and 2 claim the same" in str(exc_info.value.detail)


def test_filter_lenient_drops_owner_matching_several_live_resources(monkeypatch):
    # Two live resources match one owner (a reused VMID): none of that owner's
    # resources may be returned, but another VM's resource still is.
    _patch_netbox(
        monkeypatch,
        [_vm(1), _vm(2)],
        [_sidecar(1, vmid=500), _sidecar(2, vmid=503)],
    )
    resources = [
        {"cluster-a": [{"type": "qemu", "vmid": 500, "node": "pve-a"}]},
        {"cluster-a": [{"type": "qemu", "vmid": 500, "node": "pve-b"}]},
        {"cluster-a": [{"type": "qemu", "vmid": 503, "node": "pve-a"}]},
    ]

    result = _run_filter(resources, [1, 2], SelectionMode.LENIENT)

    assert list(result) == [
        {"cluster-a": []},
        {"cluster-a": []},
        {"cluster-a": [{"type": "qemu", "vmid": 503, "node": "pve-a"}]},
    ]
    assert [skip["netbox_vm_id"] for skip in selection_skips(result)] == [1]
    assert "Multiple live Proxmox resources" in str(selection_skips(result)[0]["reason"])


def test_lenient_still_fails_closed_when_netbox_omits_a_selected_vm(monkeypatch):
    _patch_netbox(monkeypatch, [_vm(GOOD)], [_sidecar(GOOD)])

    with pytest.raises(ProxboxException, match="selected VM ownership") as exc_info:
        _run_filter([], [GOOD, 99], SelectionMode.LENIENT)

    assert "NetBox did not return" in str(exc_info.value.detail)


@pytest.mark.parametrize("failure", ["unavailable", "failed"])
def test_lenient_still_fails_closed_when_the_sidecar_scan_is_unusable(monkeypatch, failure):
    async def _list(*_args, **_kwargs):
        return [_vm(GOOD)]

    async def _scan(_nb):
        return SimpleNamespace(
            rows=(),
            sidecar_unavailable=failure == "unavailable",
            sidecar_read_failed=failure == "failed",
        )

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _list)
    monkeypatch.setattr("proxbox_api.services.sync.vm_filter.load_vm_sync_state_identities", _scan)

    with pytest.raises(ProxboxException, match="sync-state lookup"):
        _run_filter([], [GOOD], SelectionMode.LENIENT)
    with pytest.raises(ProxboxException, match="sync-state lookup"):
        asyncio.run(
            hydrate_vm_identities_from_sidecars(
                object(), [_vm(GOOD)], require_all=False, mode=SelectionMode.LENIENT
            )
        )


def test_hydrate_selected_lenient_vs_strict(monkeypatch):
    _patch_netbox(
        monkeypatch,
        [],
        [
            _sidecar(GOOD),
            _sidecar(INCOMPLETE, endpoint_id=None),
            _sidecar(DUPLICATE),
            _sidecar(DUPLICATE, endpoint_id=22),
        ],
    )
    vms = [_vm(GOOD), _vm(INCOMPLETE), _vm(DUPLICATE), _vm(NO_SIDECAR)]

    lenient = asyncio.run(hydrate_selected_vm_identities(object(), vms, mode=SelectionMode.LENIENT))

    assert [vm["id"] for vm in lenient] == [GOOD]
    assert lenient[0]["proxmox_endpoint_id"] == 11
    assert sorted(skip["netbox_vm_id"] for skip in selection_skips(lenient)) == [
        INCOMPLETE,
        DUPLICATE,
        NO_SIDECAR,
    ]
    with pytest.raises(ProxboxException, match="selected VM ownership"):
        asyncio.run(hydrate_selected_vm_identities(object(), vms))


def test_hydrate_estate_scan_skips_unmanaged_vms_silently(monkeypatch, proxbox_caplog):
    # require_all=False is an estate scan: a VM with no sidecar is simply unmanaged
    # and is neither an error nor a warning. Only a present-but-unusable sidecar is.
    _patch_netbox(monkeypatch, [], [_sidecar(GOOD), _sidecar(INCOMPLETE, endpoint_id=None)])
    vms = [_vm(GOOD), _vm(INCOMPLETE), _vm(NO_SIDECAR)]

    result = asyncio.run(
        hydrate_vm_identities_from_sidecars(
            object(), vms, require_all=False, mode=SelectionMode.LENIENT
        )
    )

    assert [vm["id"] for vm in result] == [GOOD]
    assert [skip["netbox_vm_id"] for skip in selection_skips(result)] == [INCOMPLETE]
    assert not any(f"NetBox VM id {NO_SIDECAR}" in r.getMessage() for r in proxbox_caplog.records)
    with pytest.raises(ProxboxException, match="selected VM ownership"):
        asyncio.run(hydrate_vm_identities_from_sidecars(object(), vms, require_all=False))


def test_selection_skips_of_a_plain_list_is_empty():
    assert selection_skips([{"id": 1}]) == []


# Two Proxmox sources. Downstream stages pair filtered row i with pxs[i] and
# cluster_status[i], so a dropped row must never shift the other source's row.
A_VM = 21
B_VM = 22
_TWO_SOURCES = (("cluster-a", 11), ("cluster-b", 22))


def _two_source_estate(monkeypatch, *, a_valid: bool, b_valid: bool):
    vms = [_vm(A_VM, "cluster-a"), _vm(B_VM, "cluster-b")]
    sidecars = [
        _sidecar(A_VM, cluster_name="cluster-a", vmid=121, endpoint_id=11 if a_valid else None),
        _sidecar(B_VM, cluster_name="cluster-b", vmid=122, endpoint_id=22 if b_valid else None),
    ]
    _patch_netbox(monkeypatch, vms, sidecars)
    return [
        {"cluster-a": [{"type": "qemu", "vmid": 121, "name": "vm-21"}]},
        {"cluster-b": [{"type": "qemu", "vmid": 122, "name": "vm-22"}]},
    ]


def _run_two_source_filter(resources, mode=SelectionMode.LENIENT):
    pxs, statuses = _sources(*_TWO_SOURCES)
    return asyncio.run(
        filter_cluster_resources_by_netbox_vm_ids(
            object(),
            resources,
            [A_VM, B_VM],
            pxs=pxs,
            cluster_status=statuses,
            mode=mode,
        )
    )


def _row_keys(result) -> list[list[str]]:
    return [list(row) for row in result]


def test_lenient_skipping_first_source_vm_keeps_survivor_on_second_source(monkeypatch):
    resources = _two_source_estate(monkeypatch, a_valid=False, b_valid=True)

    result = _run_two_source_filter(resources)

    assert _row_keys(result) == [["cluster-a"], ["cluster-b"]]
    assert result[0] == {"cluster-a": []}
    assert result[1] == {"cluster-b": [{"type": "qemu", "vmid": 122, "name": "vm-22"}]}
    assert [skip["netbox_vm_id"] for skip in selection_skips(result)] == [A_VM]


def test_lenient_skipping_second_source_vm_keeps_survivor_on_first_source(monkeypatch):
    resources = _two_source_estate(monkeypatch, a_valid=True, b_valid=False)

    result = _run_two_source_filter(resources)

    assert result[0] == {"cluster-a": [{"type": "qemu", "vmid": 121, "name": "vm-21"}]}
    assert result[1] == {"cluster-b": []}
    assert [skip["netbox_vm_id"] for skip in selection_skips(result)] == [B_VM]


def test_both_valid_sources_are_unchanged(monkeypatch):
    resources = _two_source_estate(monkeypatch, a_valid=True, b_valid=True)

    result = _run_two_source_filter(resources)

    assert list(result) == resources
    assert selection_skips(result) == []


def test_all_skipped_keeps_every_source_row_empty(monkeypatch):
    resources = _two_source_estate(monkeypatch, a_valid=False, b_valid=False)

    result = _run_two_source_filter(resources)

    assert list(result) == [{"cluster-a": []}, {"cluster-b": []}]
    assert sorted(skip["netbox_vm_id"] for skip in selection_skips(result)) == [A_VM, B_VM]


def test_strict_selecting_only_second_source_vm_keeps_row_alignment(monkeypatch):
    resources = _two_source_estate(monkeypatch, a_valid=True, b_valid=True)
    pxs, statuses = _sources(*_TWO_SOURCES)

    result = asyncio.run(
        filter_cluster_resources_by_netbox_vm_ids(
            object(), resources, [B_VM], pxs=pxs, cluster_status=statuses
        )
    )

    assert result[0] == {"cluster-a": []}
    assert result[1] == resources[1]


def test_vm_create_endpoint_pairing_follows_source_after_lenient_drop(monkeypatch):
    """The REST create path derives each row's endpoint from its index in ``pxs``."""

    from proxbox_api.routes.virtualization.virtual_machines import sync_vm

    resources = _two_source_estate(monkeypatch, a_valid=False, b_valid=True)
    pxs, statuses = _sources(*_TWO_SOURCES)
    filtered = asyncio.run(
        sync_vm._filter_cluster_resources_by_netbox_vm_ids(
            object(),
            resources,
            [A_VM, B_VM],
            pxs=pxs,
            cluster_status=statuses,
        )
    )
    endpoint_ids = [sync_vm._session_endpoint_id_at(pxs, i) for i in range(len(filtered))]
    deduplicated = sync_vm._deduplicate_vm_resources_by_identity(filtered, endpoint_ids)

    paired = [
        (endpoint_ids[index], cluster, resource["vmid"])
        for index, row in enumerate(deduplicated)
        for cluster, rows in row.items()
        for resource in rows
    ]
    assert paired == [(22, "cluster-b", 122)]


def _shared_owner_setup(monkeypatch, claimants: list[int]):
    vms = [_vm(netbox_id) for netbox_id in [*claimants, GOOD]]
    sidecars = [_sidecar(netbox_id, vmid=500) for netbox_id in claimants]
    sidecars.append(_sidecar(GOOD))
    _patch_netbox(monkeypatch, vms, sidecars)
    return vms


@pytest.mark.parametrize("hydrate", ["selected", "sidecars"])
@pytest.mark.parametrize("claimants", [[20, 21], [20, 21, 22]])
def test_hydrate_lenient_drops_every_claimant_of_a_shared_owner(
    monkeypatch, proxbox_caplog, hydrate, claimants
):
    vms = _shared_owner_setup(monkeypatch, claimants)

    if hydrate == "selected":
        result = asyncio.run(
            hydrate_selected_vm_identities(object(), vms, mode=SelectionMode.LENIENT)
        )
    else:
        result = asyncio.run(
            hydrate_vm_identities_from_sidecars(
                object(), vms, require_all=False, mode=SelectionMode.LENIENT
            )
        )

    assert [vm["id"] for vm in result] == [GOOD]
    assert sorted(skip["netbox_vm_id"] for skip in selection_skips(result)) == claimants
    reasons = {skip["reason"] for skip in selection_skips(result)}
    assert len(reasons) == 1
    reason = reasons.pop()
    assert all(str(netbox_id) in reason for netbox_id in claimants)
    assert "VMID 500" in reason
    for netbox_id in claimants:
        assert any(
            f"NetBox VM id {netbox_id} " in record.getMessage() for record in proxbox_caplog.records
        )


@pytest.mark.parametrize("hydrate", ["selected", "sidecars"])
def test_hydrate_strict_raises_on_a_shared_owner(monkeypatch, hydrate):
    vms = _shared_owner_setup(monkeypatch, [20, 21])

    with pytest.raises(ProxboxException, match="claim the same Proxmox"):
        if hydrate == "selected":
            asyncio.run(hydrate_selected_vm_identities(object(), vms))
        else:
            asyncio.run(hydrate_vm_identities_from_sidecars(object(), vms, require_all=True))


def test_hydrate_same_endpoint_and_vmid_in_different_clusters_is_not_shared(monkeypatch):
    vms = [_vm(20, "cluster-a"), _vm(21, "cluster-b")]
    sidecars = [
        _sidecar(20, cluster_name="cluster-a", vmid=500),
        _sidecar(21, cluster_name="cluster-b", vmid=500),
    ]
    _patch_netbox(monkeypatch, vms, sidecars)

    result = asyncio.run(hydrate_selected_vm_identities(object(), vms))

    assert [vm["id"] for vm in result] == [20, 21]
    assert selection_skips(result) == []


def test_hydrate_cluster_name_case_does_not_hide_a_shared_owner(monkeypatch):
    vms = [_vm(20, "Cluster-A"), _vm(21, "cluster-a")]
    sidecars = [
        _sidecar(20, cluster_name="Cluster-A", vmid=500),
        _sidecar(21, cluster_name="cluster-a", vmid=500),
    ]
    _patch_netbox(monkeypatch, vms, sidecars)

    result = asyncio.run(hydrate_selected_vm_identities(object(), vms, mode=SelectionMode.LENIENT))

    assert list(result) == []
    assert sorted(skip["netbox_vm_id"] for skip in selection_skips(result)) == [20, 21]


def test_hydrate_different_vm_type_or_endpoint_is_not_shared(monkeypatch):
    vms = [_vm(20), _vm(21), _vm(22)]
    sidecars = [
        _sidecar(20, vmid=500),
        _sidecar(21, vmid=500, vm_type="lxc"),
        _sidecar(22, vmid=500, endpoint_id=99),
    ]
    _patch_netbox(monkeypatch, vms, sidecars)

    result = asyncio.run(hydrate_selected_vm_identities(object(), vms))

    assert [vm["id"] for vm in result] == [20, 21, 22]


def _run_hydrate(hydrate: str, vms, *, mode: SelectionMode):
    if hydrate == "selected":
        return asyncio.run(hydrate_selected_vm_identities(object(), vms, mode=mode))
    return asyncio.run(
        hydrate_vm_identities_from_sidecars(object(), vms, require_all=True, mode=mode)
    )


def _unselected_claimant_setup(monkeypatch, *, selected: list[int], sidecars):
    """Patch NetBox so every VM with a sidecar exists but only ``selected`` is requested."""

    all_vms = [_vm(sidecar["virtual_machine"]["id"]) for sidecar in sidecars]
    _patch_netbox(monkeypatch, all_vms, sidecars)
    return [vm for vm in all_vms if vm["id"] in selected]


@pytest.mark.parametrize("hydrate", ["selected", "sidecars"])
def test_hydrate_lenient_skips_selected_vm_sharing_owner_with_unselected_claimant(
    monkeypatch, proxbox_caplog, hydrate
):
    vms = _unselected_claimant_setup(
        monkeypatch,
        selected=[20],
        sidecars=[_sidecar(20, vmid=500), _sidecar(21, vmid=500)],
    )

    result = _run_hydrate(hydrate, vms, mode=SelectionMode.LENIENT)

    assert list(result) == []
    skips = selection_skips(result)
    # Only the selected claimant is reported; the unselected one is not processed.
    assert [skip["netbox_vm_id"] for skip in skips] == [20]
    assert "NetBox VM ids 20 and 21 claim the same" in skips[0]["reason"]
    assert any("NetBox VM id 20 " in record.getMessage() for record in proxbox_caplog.records)


@pytest.mark.parametrize("hydrate", ["selected", "sidecars"])
def test_hydrate_strict_raises_when_unselected_claimant_shares_owner(monkeypatch, hydrate):
    vms = _unselected_claimant_setup(
        monkeypatch,
        selected=[20],
        sidecars=[_sidecar(20, vmid=500), _sidecar(21, vmid=500)],
    )

    with pytest.raises(ProxboxException, match="NetBox VM ids 20 and 21 claim the same"):
        _run_hydrate(hydrate, vms, mode=SelectionMode.STRICT)


@pytest.mark.parametrize("hydrate", ["selected", "sidecars"])
def test_hydrate_three_claimants_two_selected_names_all_claimants(monkeypatch, hydrate):
    vms = _unselected_claimant_setup(
        monkeypatch,
        selected=[20, 21],
        sidecars=[_sidecar(20, vmid=500), _sidecar(21, vmid=500), _sidecar(22, vmid=500)],
    )

    result = _run_hydrate(hydrate, vms, mode=SelectionMode.LENIENT)

    assert list(result) == []
    skips = selection_skips(result)
    assert sorted(skip["netbox_vm_id"] for skip in skips) == [20, 21]
    assert all("NetBox VM ids 20, 21, 22" in skip["reason"] for skip in skips)


@pytest.mark.parametrize("hydrate", ["selected", "sidecars"])
def test_hydrate_unselected_claimant_with_incomplete_identity_does_not_count(monkeypatch, hydrate):
    vms = _unselected_claimant_setup(
        monkeypatch,
        selected=[20],
        sidecars=[_sidecar(20, vmid=500), _sidecar(21, vmid=500, endpoint_id=None)],
    )

    result = _run_hydrate(hydrate, vms, mode=SelectionMode.STRICT)

    assert [vm["id"] for vm in result] == [20]
    assert selection_skips(result) == []


@pytest.mark.parametrize("hydrate", ["selected", "sidecars"])
@pytest.mark.parametrize(
    "other",
    [
        {"cluster_name": "cluster-b"},
        {"vm_type": "lxc"},
        {"endpoint_id": 99},
    ],
)
def test_hydrate_unselected_claimant_with_different_owner_is_not_shared(
    monkeypatch, hydrate, other
):
    vms = _unselected_claimant_setup(
        monkeypatch,
        selected=[20],
        sidecars=[_sidecar(20, vmid=500), _sidecar(21, vmid=500, **other)],
    )

    result = _run_hydrate(hydrate, vms, mode=SelectionMode.STRICT)

    assert [vm["id"] for vm in result] == [20]
    assert selection_skips(result) == []


def _run_selected_vm_filter(vm, resources):
    pxs, statuses = _sources(("cluster-a", 11))
    return asyncio.run(
        filter_cluster_resources_for_selected_vm(
            vm,
            resources,
            netbox_session=object(),
            netbox_vm_id=vm["id"],
            pxs=pxs,
            cluster_status=statuses,
        )
    )


_SHARED_RESOURCES = [{"cluster-a": [{"type": "qemu", "vmid": 500, "name": "shared"}]}]


@pytest.mark.parametrize("mode", [SelectionMode.STRICT, SelectionMode.LENIENT])
def test_filter_by_ids_rejects_selected_vm_sharing_owner_with_unselected_claimant(
    monkeypatch, mode
):
    _unselected_claimant_setup(
        monkeypatch,
        selected=[20],
        sidecars=[_sidecar(20, vmid=500), _sidecar(21, vmid=500)],
    )

    if mode is SelectionMode.STRICT:
        with pytest.raises(ProxboxException) as exc_info:
            _run_filter(_SHARED_RESOURCES, [20], mode)
        assert "NetBox VM ids 20 and 21 claim the same" in str(exc_info.value.detail)
        return

    result = _run_filter(_SHARED_RESOURCES, [20], mode)
    assert list(result) == [{"cluster-a": []}]
    assert [skip["netbox_vm_id"] for skip in selection_skips(result)] == [20]


@pytest.mark.parametrize("mode", [SelectionMode.STRICT, SelectionMode.LENIENT])
def test_filter_by_ids_keeps_vm_when_other_sidecar_is_not_a_claimant(monkeypatch, mode):
    _unselected_claimant_setup(
        monkeypatch,
        selected=[20],
        sidecars=[_sidecar(20, vmid=500), _sidecar(21, vmid=501)],
    )

    result = _run_filter(_SHARED_RESOURCES, [20], mode)

    assert list(result) == _SHARED_RESOURCES
    assert selection_skips(result) == []


def test_single_vm_filter_rejects_owner_shared_with_unselected_claimant(monkeypatch):
    vms = _unselected_claimant_setup(
        monkeypatch,
        selected=[20],
        sidecars=[_sidecar(20, vmid=500), _sidecar(21, vmid=500)],
    )

    with pytest.raises(ProxboxException) as exc_info:
        _run_selected_vm_filter(vms[0], _SHARED_RESOURCES)

    assert "NetBox VM ids 20 and 21 claim the same" in str(exc_info.value.detail)


def test_single_vm_filter_keeps_vm_when_other_sidecar_is_not_a_claimant(monkeypatch):
    vms = _unselected_claimant_setup(
        monkeypatch,
        selected=[20],
        sidecars=[_sidecar(20, vmid=500), _sidecar(21, vmid=501)],
    )

    assert _run_selected_vm_filter(vms[0], _SHARED_RESOURCES) == _SHARED_RESOURCES
