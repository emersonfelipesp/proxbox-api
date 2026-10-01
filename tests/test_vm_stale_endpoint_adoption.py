"""Stale endpoint id self-heal and duplicate prevention for VM sync.

The endpoint id stored on a VM sidecar comes from an id space that is
independent per deployment, so a recreated endpoint leaves stored ids stale.
A VM stored under a stale id is adopted only in the unambiguous case; the tests
below pin both the adoption and every refusal path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.routes.virtualization.virtual_machines import sync_vm
from proxbox_api.services.sync import sync_state_reader
from proxbox_api.services.sync.sync_state_reader import (
    StaleEndpointAdoption,
    VMRoleSnapshotScan,
    adopt_vm_with_stale_endpoint_id,
)


@pytest.fixture
def proxbox_caplog(caplog):
    """Attach caplog to the non-propagating ``proxbox`` logger."""
    proxbox_logger = logging.getLogger("proxbox")
    proxbox_logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        proxbox_logger.removeHandler(caplog.handler)


LIVE_ENDPOINT_ID = 2
STALE_ENDPOINT_ID = 1
CLUSTER_ID = 86
CLUSTER_NAME = "cluster-live"
VMID = 100


def _vm_record(
    vm_id: int = 960,
    *,
    cluster_id: int = CLUSTER_ID,
    name: str = "n",
    endpoint_id: int | None = STALE_ENDPOINT_ID,
) -> dict[str, object]:
    record: dict[str, object] = {
        "id": vm_id,
        "name": name,
        "status": "active",
        "cluster": {
            "id": cluster_id,
            "name": CLUSTER_NAME if cluster_id == CLUSTER_ID else "other",
        },
        "proxmox_vm_id": VMID,
        "proxmox_vm_type": "qemu",
    }
    if endpoint_id is not None:
        record["proxmox_endpoint_id"] = endpoint_id
    return record


def _sidecar(
    vm_id: int = 960,
    *,
    raw_endpoint_id: int | None = STALE_ENDPOINT_ID,
    vm_type: str | None = "qemu",
    cluster_name: str = CLUSTER_NAME,
    vmid: int = VMID,
) -> dict[str, object]:
    row: dict[str, object] = {
        "virtual_machine": {"id": vm_id},
        "proxmox_vm_id": vmid,
        "proxmox_cluster_name": cluster_name,
    }
    if raw_endpoint_id is not None:
        row["proxmox_endpoint_raw_id"] = raw_endpoint_id
    if vm_type is not None:
        row["proxmox_vm_type"] = vm_type
    return row


class _FakeNetBox:
    """Serve sidecar and VM reads for the reader; capture sidecar re-binds."""

    def __init__(self, sidecars: list[dict[str, object]], vms: list[dict[str, object]]) -> None:
        self.sidecars = sidecars
        self.vms = {int(vm["id"]): vm for vm in vms}
        self.rebinds: list[tuple[int, int]] = []
        self.rebind_ok = True
        self.queries: list[dict[str, object]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _list(_nb, _path, *, query=None):
            query = dict(query or {})
            self.queries.append(query)
            rows = [
                row
                for row in self.sidecars
                if all(
                    row.get(key) == value
                    for key, value in query.items()
                    if key in {"proxmox_vm_id", "proxmox_endpoint_raw_id"}
                )
            ]
            return [dict(row) for row in rows[: int(query.get("limit", 50))]]

        async def _first(_nb, _path, *, query):
            return self.vms.get(int(query["id"]))

        async def _rebind(_nb, *, virtual_machine_id, endpoint_id):
            if not self.rebind_ok:
                return None
            self.rebinds.append((int(virtual_machine_id), int(endpoint_id)))
            for row in self.sidecars:
                if row["virtual_machine"]["id"] == virtual_machine_id:
                    row["proxmox_endpoint_raw_id"] = endpoint_id
            return {"virtual_machine": {"id": virtual_machine_id}}

        sync_state_reader.reset_sidecar_reader_availability_cache()
        monkeypatch.setattr(sync_state_reader, "rest_list_async", _list)
        monkeypatch.setattr(sync_state_reader, "rest_first_async", _first)
        monkeypatch.setattr(sync_state_reader, "write_vm_endpoint_raw_id", _rebind)


def _adoption(configured: set[int] | None = None, vm_name: str = "n") -> StaleEndpointAdoption:
    return StaleEndpointAdoption(
        vm_type="qemu",
        cluster_name=CLUSTER_NAME,
        vm_name=vm_name,
        configured_endpoint_ids=frozenset(
            configured if configured is not None else {LIVE_ENDPOINT_ID}
        ),
    )


def _loader(ids: frozenset[int] | None):
    async def _load() -> frozenset[int] | None:
        return ids

    return _load


async def _adopt(adoption: StaleEndpointAdoption | None = None):
    return await adopt_vm_with_stale_endpoint_id(
        object(),
        proxmox_vm_id=VMID,
        endpoint_id=LIVE_ENDPOINT_ID,
        cluster_id=CLUSTER_ID,
        adoption=adoption or _adoption(),
    )


@pytest.mark.asyncio
async def test_stale_endpoint_id_is_adopted_and_sidecar_rebound(
    monkeypatch, proxbox_caplog
) -> None:
    fake = _FakeNetBox([_sidecar()], [_vm_record()])
    fake.install(monkeypatch)

    with proxbox_caplog.at_level(logging.INFO):
        resolution = await _adopt()

    assert resolution is not None
    assert resolution.record_id == 960
    assert resolution.source == "sidecar"
    assert fake.rebinds == [(960, LIVE_ENDPOINT_ID)]
    assert "Adopted NetBox VM id=960" in proxbox_caplog.text
    assert "endpoint id 1 to live endpoint id 2" in proxbox_caplog.text


@pytest.mark.asyncio
async def test_missing_sidecar_endpoint_id_is_adopted(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar(raw_endpoint_id=None)], [_vm_record()])
    fake.install(monkeypatch)

    resolution = await _adopt()

    assert resolution is not None
    assert fake.rebinds == [(960, LIVE_ENDPOINT_ID)]


@pytest.mark.asyncio
async def test_blank_sidecar_cluster_name_is_adopted(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar(cluster_name="")], [_vm_record()])
    fake.install(monkeypatch)

    assert await _adopt() is not None


@pytest.mark.asyncio
async def test_cluster_name_comparison_is_casefolded(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar(cluster_name=CLUSTER_NAME.upper())], [_vm_record()])
    fake.install(monkeypatch)

    assert await _adopt() is not None


@pytest.mark.asyncio
async def test_two_candidates_in_the_cluster_are_not_adopted(monkeypatch) -> None:
    fake = _FakeNetBox(
        [_sidecar(960), _sidecar(961)],
        [_vm_record(960), _vm_record(961, name="n2")],
    )
    fake.install(monkeypatch)

    assert await _adopt() is None
    assert fake.rebinds == []


@pytest.mark.asyncio
async def test_sidecar_type_mismatch_is_not_adopted(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar(vm_type="lxc")], [_vm_record()])
    fake.install(monkeypatch)

    assert await _adopt() is None
    assert fake.rebinds == []


@pytest.mark.asyncio
async def test_missing_sidecar_type_is_not_adopted(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar(vm_type=None)], [_vm_record()])
    fake.install(monkeypatch)

    assert await _adopt() is None


@pytest.mark.asyncio
async def test_sidecar_cluster_name_mismatch_is_not_adopted(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar(cluster_name="another-cluster")], [_vm_record()])
    fake.install(monkeypatch)

    assert await _adopt() is None
    assert fake.rebinds == []


@pytest.mark.asyncio
async def test_raw_id_owned_by_another_configured_endpoint_is_not_adopted(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar(raw_endpoint_id=7)], [_vm_record()])
    fake.install(monkeypatch)

    assert await _adopt(_adoption({LIVE_ENDPOINT_ID, 7})) is None
    assert fake.rebinds == []
    # The same row is adopted once no configured endpoint owns id 7.
    assert await _adopt(_adoption({LIVE_ENDPOINT_ID})) is not None


@pytest.mark.asyncio
async def test_replacement_vm_with_a_different_name_is_not_adopted(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar()], [_vm_record()])
    fake.install(monkeypatch)

    assert await _adopt(_adoption(vm_name="replacement")) is None
    assert await _adopt(_adoption(vm_name="")) is None
    assert fake.rebinds == []
    assert await _adopt(_adoption(vm_name="N")) is not None


@pytest.mark.asyncio
async def test_sidecar_stored_proxmox_name_also_satisfies_the_name_match(monkeypatch) -> None:
    sidecar = _sidecar()
    sidecar["proxmox_vm_name"] = "Proxmox-Name"
    fake = _FakeNetBox([sidecar], [_vm_record(name="renamed-in-netbox")])
    fake.install(monkeypatch)

    assert await _adopt(_adoption(vm_name="proxmox-name")) is not None


@pytest.mark.asyncio
async def test_vm_in_another_cluster_is_not_adopted(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar(960)], [_vm_record(960, cluster_id=81)])
    fake.install(monkeypatch)

    assert await _adopt() is None
    assert fake.rebinds == []


@pytest.mark.asyncio
async def test_unpersisted_rebind_leaves_vm_unadopted(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar()], [_vm_record()])
    fake.rebind_ok = False
    fake.install(monkeypatch)

    assert await _adopt() is None


def _prepared(endpoint_id: int = LIVE_ENDPOINT_ID) -> sync_vm._PreparedVMState:
    return sync_vm._PreparedVMState(
        cluster_name=CLUSTER_NAME,
        resource={"name": "n", "vmid": VMID, "type": "qemu"},
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload={
            "name": "n",
            "status": "active",
            "cluster": CLUSTER_ID,
            "device": 10,
            "role": 20,
            "vcpus": 2,
            "memory": 2048,
            "disk": 30,
            "tags": [99],
            "description": "Synced from Proxmox node pve01",
        },
        lookup={"id": 0},
        now=datetime.now(timezone.utc),
        vm_type="qemu",
        sync_state_fields={
            "proxmox_endpoint_id": endpoint_id,
            "proxmox_vm_id": VMID,
            "proxmox_vm_type": "qemu",
        },
    )


async def _hydrate_and_resolve(
    monkeypatch: pytest.MonkeyPatch,
    fake: _FakeNetBox,
    snapshot: list[dict[str, object]],
    configured: frozenset[int] | None,
):
    fake.install(monkeypatch)
    prepared = _prepared()
    hydrated = await sync_vm._hydrate_vm_snapshot_with_sidecar_identity(
        object(),
        prepared_vms=[prepared],
        netbox_snapshot=snapshot,
        configured_endpoint_ids=None if configured is None else _loader(configured),
    )
    resolutions = await sync_vm._resolve_vm_names_pre_pass([prepared], snapshot, None)
    queue = sync_vm._build_vm_operation_queue([prepared], snapshot)
    return prepared, hydrated, resolutions, queue


@pytest.mark.asyncio
async def test_stale_endpoint_vm_is_adopted_without_suffix_or_duplicate(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar()], [_vm_record()])
    snapshot = [_vm_record()]

    prepared, hydrated, resolutions, queue = await _hydrate_and_resolve(
        monkeypatch, fake, snapshot, frozenset({LIVE_ENDPOINT_ID})
    )

    assert hydrated == 1
    assert resolutions == []
    assert prepared.desired_payload["name"] == "n"
    assert snapshot[0]["proxmox_endpoint_id"] == LIVE_ENDPOINT_ID
    assert [op.method for op in queue] == ["UPDATE"]
    assert queue[0].existing_record is snapshot[0]
    assert fake.rebinds == [(960, LIVE_ENDPOINT_ID)]


@pytest.mark.asyncio
async def test_unknown_active_sessions_keep_the_duplicate_behaviour(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar()], [_vm_record()])
    snapshot = [_vm_record()]

    prepared, hydrated, resolutions, queue = await _hydrate_and_resolve(
        monkeypatch, fake, snapshot, None
    )

    assert hydrated == 0
    assert fake.rebinds == []
    assert prepared.desired_payload["name"] == "n (2)"
    assert [r.resolved_name for r in resolutions] == ["n (2)"]
    assert [op.method for op in queue] == ["CREATE"]


@pytest.mark.asyncio
async def test_raw_id_of_active_session_keeps_the_duplicate_behaviour(monkeypatch) -> None:
    # Stored id 1 belongs to another configured endpoint: the VM must not be stolen.
    fake = _FakeNetBox([_sidecar()], [_vm_record()])
    snapshot = [_vm_record()]

    prepared, hydrated, resolutions, queue = await _hydrate_and_resolve(
        monkeypatch, fake, snapshot, frozenset({STALE_ENDPOINT_ID, LIVE_ENDPOINT_ID})
    )

    assert hydrated == 0
    assert fake.rebinds == []
    assert prepared.desired_payload["name"] == "n (2)"
    assert [op.method for op in queue] == ["CREATE"]


@pytest.mark.asyncio
async def test_adoption_does_not_change_suffixing_for_a_distinct_vmid(monkeypatch) -> None:
    # A different vmid with the same name is a genuine collision and still gets a suffix.
    fake = _FakeNetBox([_sidecar()], [_vm_record()])
    snapshot = [_vm_record()]
    other = _prepared()
    other.resource["vmid"] = 101
    other.sync_state_fields["proxmox_vm_id"] = 101
    fake.install(monkeypatch)

    resolutions = await sync_vm._resolve_vm_names_pre_pass([other], snapshot, None)

    assert other.desired_payload["name"] == "n (2)"
    assert [r.resolved_name for r in resolutions] == ["n (2)"]


def test_active_session_endpoint_ids_reads_every_session() -> None:
    class _Session:
        def __init__(self, endpoint_id: object) -> None:
            self.db_endpoint_id = endpoint_id

    assert sync_vm._active_session_endpoint_ids([_Session(3), _Session(None), _Session(9)]) == {
        3,
        9,
    }
    assert sync_vm._active_session_endpoint_ids(None) == frozenset()


@pytest.mark.asyncio
async def test_adopted_vm_is_found_by_the_endpoint_keyed_lookup_afterwards(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar()], [_vm_record()])
    fake.install(monkeypatch)
    assert await _adopt() is not None

    resolution = await sync_state_reader.resolve_virtual_machine_by_sync_state(
        object(), proxmox_vm_id=VMID, endpoint_id=LIVE_ENDPOINT_ID, cluster_id=CLUSTER_ID
    )

    assert resolution is not None and resolution.record_id == 960


@pytest.mark.asyncio
async def test_ambiguous_endpoint_lookup_never_attempts_adoption(monkeypatch) -> None:
    # Two sidecar rows for the same live endpoint and cluster: the primary lookup is
    # ambiguous, so the vmid-only scan must not run and nothing may be re-bound.
    fake = _FakeNetBox(
        [
            _sidecar(960, raw_endpoint_id=LIVE_ENDPOINT_ID),
            _sidecar(961, raw_endpoint_id=LIVE_ENDPOINT_ID),
        ],
        [_vm_record(960), _vm_record(961, name="n2")],
    )
    fake.install(monkeypatch)

    resolution = await sync_vm._resolve_vm_sidecar_identity(
        object(),
        prepared=_prepared(),
        proxmox_vmid=VMID,
        endpoint_id=LIVE_ENDPOINT_ID,
        cluster_id=CLUSTER_ID,
        configured_endpoint_ids=_loader(frozenset({LIVE_ENDPOINT_ID})),
    )

    assert resolution is None
    assert fake.rebinds == []
    assert all("proxmox_endpoint_raw_id" in query for query in fake.queries)


@pytest.mark.asyncio
async def test_adopted_vm_is_patched_at_dispatch_and_never_created(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar()], [_vm_record()])
    snapshot = [_vm_record()]
    _prepared_state, _hydrated, _resolutions, queue = await _hydrate_and_resolve(
        monkeypatch, fake, snapshot, frozenset({LIVE_ENDPOINT_ID})
    )
    patched: list[int] = []

    async def _unexpected_create(*_args, **_kwargs):
        raise AssertionError("an adopted VM must never be created again")

    async def _fake_patch(_nb, _path, record_id, payload):
        patched.append(record_id)
        return {"id": record_id, **payload}

    async def _no_role_snapshots(*_args, **_kwargs):
        return VMRoleSnapshotScan(values={})

    monkeypatch.setattr(sync_vm, "rest_create_async", _unexpected_create)
    monkeypatch.setattr(sync_vm, "rest_patch_async", _fake_patch)
    monkeypatch.setattr(sync_vm, "scan_vm_last_synced_role_ids", _no_role_snapshots)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(
        object(), queue, overwrite_vm_role=False
    )

    assert failed_keys == set()
    assert patched == [960]
    assert resolved[(CLUSTER_NAME, VMID, "qemu")]["id"] == 960


@pytest.mark.asyncio
async def test_raw_id_of_configured_endpoint_outside_this_run_is_not_adopted(monkeypatch) -> None:
    # Stored id 7 is configured and enabled but has no session in this run.
    fake = _FakeNetBox([_sidecar(raw_endpoint_id=7)], [_vm_record()])
    snapshot = [_vm_record()]

    _prepared_state, hydrated, _resolutions, queue = await _hydrate_and_resolve(
        monkeypatch, fake, snapshot, frozenset({LIVE_ENDPOINT_ID, 7})
    )

    assert hydrated == 0
    assert fake.rebinds == []
    assert [op.method for op in queue] == ["CREATE"]


@pytest.mark.asyncio
async def test_unknown_raw_id_is_adopted_when_the_inventory_is_complete(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar(raw_endpoint_id=9)], [_vm_record()])
    snapshot = [_vm_record()]

    _prepared_state, hydrated, _resolutions, _queue = await _hydrate_and_resolve(
        monkeypatch, fake, snapshot, frozenset({LIVE_ENDPOINT_ID, STALE_ENDPOINT_ID})
    )

    assert hydrated == 1
    assert fake.rebinds == [(960, LIVE_ENDPOINT_ID)]


@pytest.mark.asyncio
async def test_inventory_load_failure_never_adopts(monkeypatch) -> None:
    fake = _FakeNetBox([_sidecar()], [_vm_record()])
    fake.install(monkeypatch)
    prepared = _prepared()

    async def _failing_inventory(_nb: object) -> None:
        return None

    monkeypatch.setattr(sync_vm, "load_configured_proxmox_endpoint_ids", _failing_inventory)
    resolution = await sync_vm._resolve_vm_sidecar_identity(
        object(),
        prepared=prepared,
        proxmox_vmid=VMID,
        endpoint_id=LIVE_ENDPOINT_ID,
        cluster_id=CLUSTER_ID,
        configured_endpoint_ids=sync_vm._ConfiguredEndpointIds(object(), []),
    )

    assert resolution is None
    assert fake.rebinds == []


@pytest.mark.asyncio
async def test_configured_inventory_is_loaded_once_and_includes_active_sessions(
    monkeypatch,
) -> None:
    calls: list[object] = []

    async def _inventory(nb: object) -> frozenset[int]:
        calls.append(nb)
        return frozenset({5})

    class _Session:
        db_endpoint_id = 8

    monkeypatch.setattr(sync_vm, "load_configured_proxmox_endpoint_ids", _inventory)
    loader = sync_vm._ConfiguredEndpointIds(object(), [_Session()])

    assert await loader() == frozenset({5, 8})
    assert await loader() == frozenset({5, 8})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_configured_endpoint_inventory_unions_database_and_netbox_ids(monkeypatch) -> None:
    from proxbox_api.session import proxmox_providers

    async def _database_ids() -> set[int]:
        return {1, 2}

    async def _netbox_endpoints(_nb: object, _ids: object) -> list[object]:
        return [{"id": 2}, {"id": 30}, {"id": None}]

    monkeypatch.setattr(proxmox_providers, "_load_database_endpoint_ids", _database_ids)
    monkeypatch.setattr(proxmox_providers, "_load_netbox_endpoints", _netbox_endpoints)

    assert await proxmox_providers.load_configured_proxmox_endpoint_ids(object()) == {1, 2, 30}


@pytest.mark.asyncio
async def test_configured_endpoint_inventory_fails_closed(monkeypatch) -> None:
    from proxbox_api.session import proxmox_providers

    async def _database_ids() -> set[int]:
        return {1}

    async def _netbox_down(_nb: object, _ids: object) -> list[object]:
        raise RuntimeError("netbox unavailable")

    monkeypatch.setattr(proxmox_providers, "_load_database_endpoint_ids", _database_ids)
    monkeypatch.setattr(proxmox_providers, "_load_netbox_endpoints", _netbox_down)

    assert await proxmox_providers.load_configured_proxmox_endpoint_ids(object()) is None


@pytest.mark.asyncio
async def test_hydration_resolves_same_key_vms_of_different_clusters_separately(
    monkeypatch,
) -> None:
    calls: list[object] = []

    async def fake_resolve(nb, *, prepared, cluster_id, **kwargs):
        calls.append(cluster_id)

    monkeypatch.setattr(sync_vm, "_resolve_vm_sidecar_identity", fake_resolve)
    first = _prepared()
    second = _prepared()
    second.desired_payload = {**second.desired_payload, "cluster": CLUSTER_ID + 1}

    await sync_vm._hydrate_vm_snapshot_with_sidecar_identity(
        object(),
        prepared_vms=[first, second],
        netbox_snapshot=[],
        configured_endpoint_ids=_loader(frozenset({LIVE_ENDPOINT_ID})),
    )

    assert sorted(calls) == [CLUSTER_ID, CLUSTER_ID + 1]
