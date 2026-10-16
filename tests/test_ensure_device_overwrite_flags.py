"""Regression tests for ``_ensure_device`` honoring ``overwrite_*`` flags.

Issue #342 (`emersonfelipesp/netbox-proxbox#342`): when a Proxmox node has been
synced to NetBox and the user later changes its ``device_type`` from the default
``Proxmox Generic Device`` to a custom one, a follow-up VM sync would revert it
back. The bulk DCIM sync path was already wired through ``patchable_fields``,
but ``_ensure_device`` (the per-VM helper that materializes parent devices)
ignored every overwrite flag and applied a raw diff via ``setattr`` + ``save``.

These tests pin the fix: with ``overwrite_device_type=False`` and an existing
device, the FK is not patched. They also lock symmetric behavior for
``overwrite_device_role`` and ``overwrite_device_tags``, plus the first-create
branch propagating ``patchable_fields`` to ``rest_reconcile_async``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from netbox_sdk.client import ApiResponse

from proxbox_api import netbox_rest
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import clear_rest_get_cache, rest_bulk_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxDeviceSyncState
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync import device_ensure, sync_state_writer


class _FakeExistingDevice:
    """Stand-in for a NetBox record returned by ``rest_list_async``."""

    def __init__(self, current: dict[str, Any]) -> None:
        self._current = current
        self.saved = False
        self.applied: dict[str, Any] = {}

    # ``_record_has_tag`` reads ``serialize()`` first, then falls back to dict
    # access. Returning a dict-shaped payload without proxbox-tagged entries
    # lets ``_prefer_existing_device`` keep this record as the chosen match.
    def serialize(self) -> dict[str, Any]:
        return {**self._current}

    def get(self, key: str, default: Any = None) -> Any:
        return self._current.get(key, default)

    async def save(self) -> None:
        self.saved = True

    def __setattr__(self, key: str, value: Any) -> None:
        if key in {"_current", "saved", "applied"}:
            object.__setattr__(self, key, value)
            return
        self.applied[key] = value


def _target(*, desired_name: str = "pve01.cluster-a.example.com") -> device_ensure._DeviceTarget:
    return device_ensure._DeviceTarget(
        cluster_name="cluster-a",
        node_name="pve01",
        desired_name=desired_name,
        effective_name=desired_name,
        desired_site_id=41,
        site_id=41,
        cluster_id=11,
    )


class _FakePhaseRecord(_FakeExistingDevice):
    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__(current)
        object.__setattr__(self, "id", current.get("id"))


class _OmittingOwnershipApi:
    """Old-plugin API that never serializes the endpoint ownership field."""

    def __init__(self) -> None:
        self.client = self
        self.methods: list[str] = []

    async def request(self, method: str, path: str, **_kwargs: object) -> ApiResponse:
        assert path.startswith(sync_state_writer.DEVICE_SYNC_STATE_PATH)
        self.methods.append(method)
        row = {"id": 901, "device": {"id": 101}}
        if method == "GET":
            return ApiResponse(
                status=200,
                text=json.dumps({"count": 1, "next": None, "results": [row]}),
            )
        if method == "PATCH":
            return ApiResponse(status=200, text=json.dumps(row))
        raise AssertionError(method)


def _existing_payload(*, device_type_id: int, role_id: int, tagged: bool = True) -> dict[str, Any]:
    """Return a NetBox-shape device payload with the given FKs.

    ``tagged`` controls whether the record carries the ``proxbox`` tag, which is
    the operator opt-in that allows Proxbox to adopt a pre-existing device whose
    site/cluster differ from the Proxmox target (issue #561).
    """
    tags: list[dict[str, Any]] = (
        [{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}] if tagged else []
    )
    return {
        "id": 101,
        "name": "pve01",
        "status": "active",
        "cluster": 11,
        "device_type": device_type_id,
        "role": role_id,
        "site": 41,
        "description": "Proxmox Node pve01",
        "tags": tags,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_ensure_device_writes_endpoint_identity_on_create_and_update(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    record = _FakeExistingDevice({"id": 101, **_existing_payload(device_type_id=42, role_id=10)})
    writes: list[dict[str, object]] = []

    async def _prepare(_nb: object, targets: list[device_ensure._DeviceTarget]) -> None:
        if existing:
            targets[0].existing = record

    async def _no_tag(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _reconcile(*_args: Any, **kwargs: Any) -> dict[str, object]:
        return {"id": 202, **kwargs["payload"]}

    async def _sidecars(*_args: Any, **_kwargs: Any) -> list[dict[str, object]]:
        return [{"id": 901, "device": 101 if existing else 202, "proxmox_endpoint_raw_id": 501}]

    async def _write(*_args: Any, **kwargs: Any) -> dict[str, object]:
        writes.append(kwargs)
        return {"id": 901, **kwargs}

    monkeypatch.setattr(device_ensure, "_prepare_device_targets", _prepare)
    monkeypatch.setattr(device_ensure, "resolve_discovery_tag_id", _no_tag)
    monkeypatch.setattr(device_ensure, "rest_list_async", _sidecars)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _reconcile)
    monkeypatch.setattr(device_ensure, "write_device_sync_state", _write)

    await device_ensure._ensure_device(
        object(),
        device_name="pve01",
        cluster_name="cluster-a",
        endpoint_id=501,
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[],
    )

    assert len(writes) == 1
    assert writes[0]["proxmox_endpoint_raw_id"] == 501


def test_device_targets_keep_same_short_node_distinct_by_cluster() -> None:
    clusters = [
        SimpleNamespace(
            name=cluster,
            endpoint_name="endpoint-a",
            node_device_name_template="{node}.{cluster_slug}.example.com",
            node_list=[SimpleNamespace(name="pve01")],
        )
        for cluster in ("cluster-a", "cluster-b")
    ]
    records = {
        name: _FakePhaseRecord({"id": index, "name": name, "scope_id": 41})
        for index, name in enumerate(("cluster-a", "cluster-b"), start=11)
    }

    targets = device_ensure._build_device_targets(
        clusters,
        cluster_by_name=records,
        sites={name: _FakePhaseRecord({"id": 41}) for name in records},
    )

    assert [(target.cluster_name, target.node_name) for target in targets] == [
        ("cluster-a", "pve01"),
        ("cluster-b", "pve01"),
    ]
    assert [target.desired_name for target in targets] == [
        "pve01.cluster-a.example.com",
        "pve01.cluster-b.example.com",
    ]


@pytest.mark.asyncio
async def test_device_sidecar_identity_distinguishes_duplicate_endpoint_cluster_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = [
        device_ensure._DeviceTarget(
            cluster_name="cluster-a",
            node_name="pve01",
            desired_name=f"pve01.endpoint-{endpoint_id}",
            effective_name=f"pve01.endpoint-{endpoint_id}",
            desired_site_id=41,
            site_id=41,
            cluster_id=11,
            endpoint_id=endpoint_id,
        )
        for endpoint_id in (501, 502)
    ]
    queries: list[dict[str, object]] = []

    async def _sidecars(
        _nb: object, _path: str, *, query: dict[str, object]
    ) -> list[dict[str, object]]:
        queries.append(query)
        if "device_id" in query:
            device_id = int(query["device_id"])
            return [
                {
                    "device": device_id,
                    "proxmox_endpoint_raw_id": device_id - 1000,
                    "proxmox_cluster_name": "cluster-a",
                    "proxmox_node_name": "pve01",
                }
            ]
        endpoint_id = int(query["proxmox_endpoint_raw_id"])
        return [
            {
                "device": endpoint_id + 1000,
                "proxmox_endpoint_raw_id": endpoint_id,
                "proxmox_cluster_name": "cluster-a",
                "proxmox_node_name": "pve01",
            }
        ]

    async def _device(_nb: object, _path: str, *, query: dict[str, object]) -> _FakeExistingDevice:
        return _FakeExistingDevice({"id": query["id"]})

    monkeypatch.setattr(device_ensure, "rest_list_async", _sidecars)
    monkeypatch.setattr(device_ensure, "rest_first_async", _device)

    resolved = [
        await device_ensure._device_from_identity_sidecar(object(), target) for target in targets
    ]

    assert [record.get("id") for record in resolved if record is not None] == [1501, 1502]
    identity_queries = [query for query in queries if "proxmox_endpoint_raw_id" in query]
    assert [query["proxmox_endpoint_raw_id"] for query in identity_queries] == [501, 502]


@pytest.mark.asyncio
async def test_device_sidecar_missing_endpoint_field_is_unclaimed_for_old_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(desired_name="pve01")
    target.endpoint_id = 501
    existing = _FakeExistingDevice({"id": 101, "name": "pve01", "site": 41, "cluster": 11})

    async def _records(_nb: object, path: str, *, query: dict[str, object]) -> list[Any]:
        if path == device_ensure.DEVICE_SYNC_STATE_PATH:
            return [{"id": 901, "device": 101}]
        return [existing]

    async def _device(*_args: Any, **_kwargs: Any) -> _FakeExistingDevice:
        return existing

    monkeypatch.setattr(device_ensure, "rest_list_async", _records)
    monkeypatch.setattr(device_ensure, "rest_first_async", _device)

    resolved = await device_ensure._resolve_existing_device_for_target(object(), target)

    assert resolved is existing
    assert target.ownership_claim_required is True


@pytest.mark.asyncio
async def test_bulk_sync_proceeds_when_real_writer_cannot_observe_legacy_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _OmittingOwnershipApi()
    existing = _FakePhaseRecord(_existing_payload(device_type_id=42, role_id=10))
    target = _target(desired_name="pve01")
    target.endpoint_id = 501

    async def _bulk(*_args: Any, **_kwargs: Any) -> dict[str, SimpleNamespace]:
        return {"devices": SimpleNamespace(records=[existing])}

    netbox_rest._reset_netbox_globals()
    sync_state_writer.reset_sidecar_availability_cache()
    monkeypatch.setattr(device_ensure, "rest_bulk_reconcile_phases_async", _bulk)

    devices = await device_ensure._reconcile_device_targets(
        api,
        targets=[target],
        device_type_id=42,
        role_id=10,
        tag_refs=existing.get("tags"),
        patchable_fields=set(),
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert devices[(501, "cluster-a", "pve01")] is existing
    assert target.name_conflict is False
    assert api.methods.count("PATCH") == 2


@pytest.mark.asyncio
async def test_single_sync_proceeds_when_real_writer_cannot_observe_legacy_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _OmittingOwnershipApi()
    existing = _FakeExistingDevice(_existing_payload(device_type_id=42, role_id=10))

    async def _prepare(_nb: object, targets: list[device_ensure._DeviceTarget]) -> None:
        targets[0].existing = existing
        targets[0].ownership_claim_required = True

    async def _no_tag(*_args: Any, **_kwargs: Any) -> None:
        return None

    netbox_rest._reset_netbox_globals()
    sync_state_writer.reset_sidecar_availability_cache()
    monkeypatch.setattr(device_ensure, "_prepare_device_targets", _prepare)
    monkeypatch.setattr(device_ensure, "resolve_discovery_tag_id", _no_tag)

    result = await device_ensure._ensure_device(
        api,
        device_name="pve01",
        cluster_name="cluster-a",
        endpoint_id=501,
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=existing.get("tags"),
    )

    assert result is existing
    assert existing.saved is False
    assert api.methods.count("PATCH") == 2


@pytest.mark.asyncio
async def test_unclaimed_identity_sidecar_requires_name_placement_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(desired_name="pve01")
    target.endpoint_id = 501
    sidecar_device = _FakeExistingDevice(
        {"id": 999, "name": "different-name", "site": 99, "cluster": 77}
    )

    async def _records(_nb: object, path: str, *, query: dict[str, object]) -> list[Any]:
        if path == device_ensure.DEVICE_SYNC_STATE_PATH:
            return [{"id": 901, "device": 999, "proxmox_endpoint_raw_id": None}]
        return []

    async def _device(*_args: Any, **_kwargs: Any) -> _FakeExistingDevice:
        return sidecar_device

    monkeypatch.setattr(device_ensure, "rest_list_async", _records)
    monkeypatch.setattr(device_ensure, "rest_first_async", _device)

    resolved = await device_ensure._resolve_existing_device_for_target(object(), target)

    assert resolved is None


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_path", ["bulk", "single"])
@pytest.mark.parametrize("endpoint_field_present", [False, True], ids=["missing", "null"])
async def test_renamed_legacy_identity_is_adopted_and_stamped(
    monkeypatch: pytest.MonkeyPatch,
    sync_path: str,
    endpoint_field_present: bool,
) -> None:
    existing = _FakePhaseRecord(
        {
            **_existing_payload(device_type_id=42, role_id=10),
            "name": "hypervisor-east",
            "description": "Operator managed",
            "tags": [{"slug": "proxbox"}],
        }
    )
    claimed_endpoint_id: int | None = None
    writes: list[dict[str, object]] = []

    def _sidecar() -> dict[str, object]:
        row: dict[str, object] = {
            "id": 901,
            "device": 101,
            "proxmox_cluster_name": "cluster-a",
            "proxmox_node_name": "pve01",
        }
        if endpoint_field_present or claimed_endpoint_id is not None:
            row["proxmox_endpoint_raw_id"] = claimed_endpoint_id
        return row

    async def _records(_nb: object, path: str, *, query: dict[str, object]) -> list[Any]:
        if path == device_ensure.DEVICE_SYNC_STATE_PATH:
            return [_sidecar()]
        return []

    async def _device(*_args: Any, **_kwargs: Any) -> _FakePhaseRecord:
        return existing

    async def _write(*_args: Any, **kwargs: object) -> dict[str, object]:
        nonlocal claimed_endpoint_id
        writes.append(kwargs)
        claimed_endpoint_id = int(kwargs["proxmox_endpoint_raw_id"])
        return {"id": 901, **_sidecar()}

    async def _bulk(
        _nb: object,
        phases: list[Any],
    ) -> dict[str, SimpleNamespace]:
        assert phases[0].payloads[0]["name"] == "hypervisor-east"
        return {"devices": SimpleNamespace(records=[existing])}

    async def _unexpected_reconcile(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("renamed legacy device must not be recreated")

    monkeypatch.setattr(device_ensure, "rest_list_async", _records)
    monkeypatch.setattr(device_ensure, "rest_first_async", _device)
    monkeypatch.setattr(device_ensure, "write_device_sync_state", _write)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _unexpected_reconcile)

    if sync_path == "single":
        result = await device_ensure._ensure_device(
            object(),
            device_name="pve01",
            cluster_name="cluster-a",
            endpoint_id=501,
            cluster_id=11,
            device_type_id=42,
            role_id=10,
            site_id=41,
            tag_refs=[{"slug": "proxbox"}],
        )
        assert result.get("id") == 101
    else:
        target = _target(desired_name="pve01.cluster-a.example.com")
        target.endpoint_id = 501
        await device_ensure._prepare_device_targets(object(), [target])
        monkeypatch.setattr(device_ensure, "rest_bulk_reconcile_phases_async", _bulk)
        devices = await device_ensure._reconcile_device_targets(
            object(),
            targets=[target],
            device_type_id=42,
            role_id=10,
            tag_refs=[{"slug": "proxbox"}],
            patchable_fields={"name", "cluster", "device_type", "role"},
            overwrite_flags=SyncOverwriteFlags(),
        )
        assert devices[(501, "cluster-a", "pve01")].get("id") == 101

    assert claimed_endpoint_id == 501
    assert writes[0]["proxmox_endpoint_raw_id"] == 501
    assert existing.saved is False


@pytest.mark.asyncio
async def test_name_fallback_adopts_unclaimed_device_and_stamps_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _FakeExistingDevice({"id": 101, **_existing_payload(device_type_id=42, role_id=10)})
    sidecar_writes: list[dict[str, object]] = []
    claimed_endpoint_id: int | None = None

    async def _records(_nb: object, path: str, *, query: dict[str, object]) -> list[Any]:
        if path == device_ensure.DEVICE_SYNC_STATE_PATH:
            if "device_id" in query:
                return [
                    {
                        "id": 901,
                        "device": 101,
                        "proxmox_endpoint_raw_id": claimed_endpoint_id,
                    }
                ]
            return []
        return [existing]

    async def _write(*_args: Any, **kwargs: object) -> dict[str, object]:
        nonlocal claimed_endpoint_id
        if not sidecar_writes:
            assert existing.saved is False
        sidecar_writes.append(kwargs)
        claimed_endpoint_id = int(kwargs["proxmox_endpoint_raw_id"])
        return {"id": 901, **kwargs}

    monkeypatch.setattr(device_ensure, "rest_list_async", _records)
    monkeypatch.setattr(device_ensure, "write_device_sync_state", _write)

    result = await device_ensure._ensure_device(
        object(),
        device_name="pve01",
        cluster_name="cluster-a",
        endpoint_id=502,
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"slug": "proxbox"}],
        overwrite_flags=SyncOverwriteFlags(overwrite_device_custom_fields=False),
    )

    assert result is existing
    assert len(sidecar_writes) == 2
    assert sidecar_writes[0]["proxmox_endpoint_raw_id"] == 502
    assert sidecar_writes[0]["overwrite_custom_fields"] is False
    assert sidecar_writes[1]["proxmox_endpoint_raw_id"] == 502


@pytest.mark.asyncio
async def test_bulk_second_endpoint_marks_foreign_owned_name_as_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _FakeExistingDevice({"id": 101, **_existing_payload(device_type_id=42, role_id=10)})
    target = _target(desired_name="pve01")
    target.endpoint_id = 502

    async def _records(_nb: object, path: str, *, query: dict[str, object]) -> list[Any]:
        if path == device_ensure.DEVICE_SYNC_STATE_PATH:
            return [
                {
                    "id": 901,
                    "device": 101,
                    "proxmox_endpoint_raw_id": 501,
                }
            ]
        return [existing]

    async def _bulk(*args: Any, **kwargs: Any) -> dict[str, Any]:
        phases = kwargs["phases"] if "phases" in kwargs else args[1]
        assert phases[0].payloads == []
        return {"devices": SimpleNamespace(records=[])}

    async def _unexpected_write(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("foreign-owned device sidecar must not be rewritten")

    monkeypatch.setattr(device_ensure, "rest_list_async", _records)
    monkeypatch.setattr(device_ensure, "rest_bulk_reconcile_phases_async", _bulk)
    monkeypatch.setattr(device_ensure, "write_device_sync_state", _unexpected_write)

    await device_ensure._prepare_device_targets(object(), [target])
    devices = await device_ensure._reconcile_device_targets(
        object(),
        targets=[target],
        device_type_id=42,
        role_id=10,
        tag_refs=[{"slug": "proxbox"}],
        patchable_fields={"name", "cluster"},
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert target.name_conflict is True
    assert devices == {}
    assert existing.saved is False
    assert existing.applied == {}


@pytest.mark.asyncio
async def test_missing_target_endpoint_still_rejects_foreign_owned_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(desired_name="pve01")
    existing = _FakeExistingDevice(_existing_payload(device_type_id=42, role_id=10))

    async def _records(_nb: object, path: str, *, query: dict[str, object]) -> list[Any]:
        if path == device_ensure.DEVICE_SYNC_STATE_PATH:
            return [
                {
                    "id": 901,
                    "device": 101,
                    "proxmox_endpoint_raw_id": 501,
                }
            ]
        return [existing]

    monkeypatch.setattr(device_ensure, "rest_list_async", _records)

    await device_ensure._prepare_device_targets(object(), [target])

    assert target.name_conflict is True
    assert target.existing is None
    assert existing.saved is False
    assert existing.applied == {}


@pytest.mark.asyncio
async def test_candidate_ownership_read_bypasses_stale_same_owner_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(desired_name="pve01.renamed")
    target.endpoint_id = 502
    existing = _FakeExistingDevice(_existing_payload(device_type_id=42, role_id=10))
    cache_clears: list[str] = []

    def _clear(_nb: object, path: str) -> None:
        cache_clears.append(path)

    async def _records(_nb: object, path: str, *, query: dict[str, object]) -> list[Any]:
        if path == device_ensure.DEVICE_SYNC_STATE_PATH:
            endpoint_id = 501 if cache_clears else 502
            return [
                {
                    "id": 901,
                    "device": 101,
                    "proxmox_endpoint_raw_id": endpoint_id,
                }
            ]
        return [existing]

    async def _device(*_args: Any, **_kwargs: Any) -> _FakeExistingDevice:
        return existing

    monkeypatch.setattr(device_ensure, "clear_rest_get_cache_for_path", _clear)
    monkeypatch.setattr(device_ensure, "rest_list_async", _records)
    monkeypatch.setattr(device_ensure, "rest_first_async", _device)

    await device_ensure._prepare_device_targets(object(), [target])

    assert cache_clears == [device_ensure.DEVICE_SYNC_STATE_PATH] * 3
    assert target.name_conflict is True
    assert existing.saved is False
    assert existing.applied == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("rendered_unclaimed", [False, True], ids=["free", "unclaimed"])
async def test_foreign_short_name_does_not_block_rendered_destination(
    monkeypatch: pytest.MonkeyPatch,
    rendered_unclaimed: bool,
) -> None:
    target = _target(desired_name="pve01.cluster-b")
    target.endpoint_id = 502
    foreign = _FakeExistingDevice(
        {**_existing_payload(device_type_id=42, role_id=10), "cluster": 11}
    )
    rendered = _FakeExistingDevice(
        {
            **_existing_payload(device_type_id=42, role_id=10),
            "id": 202,
            "name": "pve01.cluster-b",
        }
    )
    queried_names: list[str] = []

    async def _records(_nb: object, path: str, *, query: dict[str, object]) -> list[Any]:
        if path == device_ensure.DEVICE_SYNC_STATE_PATH:
            if "device_id" not in query:
                return []
            device_id = int(query["device_id"])
            return [
                {
                    "id": 900 + device_id,
                    "device": device_id,
                    "proxmox_endpoint_raw_id": 501 if device_id == 101 else None,
                }
            ]
        name = str(query["name"])
        queried_names.append(name)
        if name == "pve01":
            return [foreign]
        if name == "pve01.cluster-b" and rendered_unclaimed:
            return [rendered]
        return []

    monkeypatch.setattr(device_ensure, "rest_list_async", _records)

    resolved = await device_ensure._resolve_existing_device_for_target(object(), target)

    assert queried_names == ["pve01", "pve01.cluster-b"]
    assert resolved is (rendered if rendered_unclaimed else None)
    assert target.ownership_claim_required is rendered_unclaimed
    assert foreign.saved is False
    assert foreign.applied == {}


@pytest.mark.asyncio
async def test_bulk_candidate_appearing_after_prepare_is_checked_before_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(desired_name="pve01")
    target.endpoint_id = 502
    concurrent = _FakeExistingDevice(
        {
            **_existing_payload(device_type_id=99, role_id=98),
            "status": "offline",
        }
    )

    async def _bulk(
        _nb: object,
        phases: list[Any],
    ) -> dict[str, SimpleNamespace]:
        assert phases[0].patchable_fields == frozenset()
        return {"devices": SimpleNamespace(records=[concurrent])}

    async def _sidecars(*_args: Any, **_kwargs: Any) -> list[dict[str, object]]:
        return [{"id": 901, "device": 101, "proxmox_endpoint_raw_id": 501}]

    async def _unexpected(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("foreign race winner must not be patched or re-stamped")

    monkeypatch.setattr(device_ensure, "rest_bulk_reconcile_phases_async", _bulk)
    monkeypatch.setattr(device_ensure, "rest_list_async", _sidecars)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _unexpected)
    monkeypatch.setattr(device_ensure, "write_device_sync_state", _unexpected)

    devices = await device_ensure._reconcile_device_targets(
        object(),
        targets=[target],
        device_type_id=42,
        role_id=10,
        tag_refs=[{"slug": "proxbox"}],
        patchable_fields={"status", "device_type", "role"},
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert devices == {}
    assert target.name_conflict is True
    assert concurrent.saved is False
    assert concurrent.applied == {}


@pytest.mark.asyncio
async def test_bulk_device_patch_failure_does_not_abandon_later_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = [
        device_ensure._DeviceTarget(
            cluster_name="cluster-a",
            node_name=f"pve0{index}",
            desired_name=f"pve0{index}",
            effective_name=f"pve0{index}",
            desired_site_id=41,
            site_id=41,
            cluster_id=11,
            endpoint_id=500 + index,
        )
        for index in (1, 2)
    ]
    records = [
        _FakePhaseRecord(
            {
                **_existing_payload(device_type_id=99, role_id=98),
                "id": 100 + index,
                "name": f"pve0{index}",
                "status": "offline",
            }
        )
        for index in (1, 2)
    ]
    patch_attempts: list[int] = []

    async def _bulk(*_args: Any, **_kwargs: Any) -> dict[str, SimpleNamespace]:
        return {"devices": SimpleNamespace(records=records)}

    async def _sidecars(
        _nb: object, _path: str, *, query: dict[str, object]
    ) -> list[dict[str, object]]:
        device_id = int(query["device_id"])
        return [
            {
                "id": 900 + device_id,
                "device": device_id,
                "proxmox_endpoint_raw_id": device_id + 400,
            }
        ]

    async def _reconcile(*_args: Any, **kwargs: Any) -> _FakePhaseRecord:
        record = kwargs["existing_record"]
        patch_attempts.append(int(record.get("id")))
        if record.get("id") == 101:
            raise ProxboxException(message="NetBox rejected the first device patch")
        return record

    async def _write(*_args: Any, **kwargs: Any) -> dict[str, object]:
        return {"id": 999, **kwargs}

    monkeypatch.setattr(device_ensure, "rest_bulk_reconcile_phases_async", _bulk)
    monkeypatch.setattr(device_ensure, "rest_list_async", _sidecars)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _reconcile)
    monkeypatch.setattr(device_ensure, "write_device_sync_state", _write)

    devices = await device_ensure._reconcile_device_targets(
        object(),
        targets=targets,
        device_type_id=42,
        role_id=10,
        tag_refs=[{"slug": "proxbox"}],
        patchable_fields={"status", "device_type", "role"},
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert patch_attempts == [101, 102]
    assert targets[0].name_conflict is True
    assert targets[1].name_conflict is False
    assert set(devices) == {(502, "cluster-a", "pve02")}


@pytest.mark.asyncio
async def test_single_candidate_appearing_after_prepare_is_checked_before_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concurrent = _FakeExistingDevice(
        {
            **_existing_payload(device_type_id=99, role_id=98),
            "status": "offline",
        }
    )
    reconcile_calls = 0

    async def _prepare(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _no_tag(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _reconcile(*_args: Any, **kwargs: Any) -> _FakeExistingDevice:
        nonlocal reconcile_calls
        reconcile_calls += 1
        assert kwargs["patchable_fields"] == frozenset()
        return concurrent

    async def _sidecars(*_args: Any, **_kwargs: Any) -> list[dict[str, object]]:
        return [{"id": 901, "device": 101, "proxmox_endpoint_raw_id": 501}]

    async def _unexpected(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("foreign race winner must not be patched or re-stamped")

    monkeypatch.setattr(device_ensure, "_prepare_device_targets", _prepare)
    monkeypatch.setattr(device_ensure, "resolve_discovery_tag_id", _no_tag)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _reconcile)
    monkeypatch.setattr(device_ensure, "rest_list_async", _sidecars)
    monkeypatch.setattr(device_ensure, "write_device_sync_state", _unexpected)

    with pytest.raises(ProxboxException, match="rendered device name 'pve01' is already used"):
        await device_ensure._ensure_device(
            object(),
            device_name="pve01",
            cluster_name="cluster-a",
            endpoint_id=502,
            cluster_id=11,
            device_type_id=42,
            role_id=10,
            site_id=41,
            tag_refs=[{"slug": "proxbox"}],
        )

    assert reconcile_calls == 1
    assert concurrent.saved is False
    assert concurrent.applied == {}


@pytest.mark.asyncio
async def test_sidecar_lookup_error_fails_closed_before_name_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(desired_name="pve01")
    target.endpoint_id = 502
    calls: list[dict[str, object]] = []

    async def _failure(_nb: object, path: str, *, query: dict[str, object]) -> list[Any]:
        calls.append(query)
        assert path == device_ensure.DEVICE_SYNC_STATE_PATH
        raise TimeoutError("sidecar timeout")

    monkeypatch.setattr(device_ensure, "rest_list_async", _failure)

    await device_ensure._prepare_device_targets(object(), [target])

    assert target.name_conflict is True
    assert calls == [
        {
            "proxmox_cluster_name": "cluster-a",
            "proxmox_node_name": "pve01",
            "limit": 3,
            "proxmox_endpoint_raw_id": 502,
        }
    ]


@pytest.mark.asyncio
async def test_plain_sidecar_not_found_keeps_name_fallback_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(desired_name="pve01")
    target.endpoint_id = 502
    existing = _FakeExistingDevice({"id": 101, "name": "pve01", "site": 41, "cluster": 11})

    async def _records(_nb: object, path: str, *, query: dict[str, object]) -> list[Any]:
        if path == device_ensure.DEVICE_SYNC_STATE_PATH:
            raise ProxboxException(
                message="Not found",
                detail="Not found.",
                http_status_code=404,
            )
        return [existing]

    monkeypatch.setattr(device_ensure, "rest_list_async", _records)

    resolved = await device_ensure._resolve_existing_device_for_target(object(), target)

    assert resolved is existing


@pytest.mark.asyncio
async def test_sidecar_write_conflict_maps_to_bulk_and_single_name_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(desired_name="pve01")
    target.endpoint_id = 502
    record = _FakeExistingDevice({"id": 101, "name": "pve01", "site": 41, "cluster": 11})

    async def _conflict(*_args: Any, **_kwargs: Any) -> None:
        raise device_ensure.DeviceSyncStateOwnershipConflict(
            device_id=101,
            expected_endpoint_id=502,
            actual_endpoint_id=501,
        )

    monkeypatch.setattr(device_ensure, "write_device_sync_state", _conflict)

    persisted = await device_ensure._write_device_target_sync_state(
        object(),
        target=target,
        record=record,
        payload={"name": "pve01"},
        overwrite_custom_fields=True,
    )

    assert persisted is False
    assert target.name_conflict is True

    with pytest.raises(ProxboxException, match="rendered device name 'pve01' is already used"):
        await device_ensure._write_device_target_sync_state_or_raise(
            object(),
            target=target,
            record=record,
            payload={"name": "pve01"},
            overwrite_custom_fields=True,
        )


@pytest.mark.asyncio
async def test_failed_unclaimed_sidecar_stamp_becomes_name_conflict_before_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(desired_name="pve01.new-name")
    target.endpoint_id = 502
    target.ownership_claim_required = True
    target.existing = _FakeExistingDevice(
        {
            "id": 101,
            "name": "pve01",
            "site": 41,
            "cluster": 11,
            "description": "Proxmox Node pve01",
            "tags": [{"slug": "proxbox"}],
        }
    )

    async def _failed_write(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(device_ensure, "write_device_sync_state", _failed_write)

    await device_ensure._claim_unclaimed_device_targets(object(), [target])

    assert target.name_conflict is True
    assert target.ownership_claim_required is True
    assert target.existing.saved is False
    assert target.existing.applied == {}


@pytest.mark.asyncio
async def test_lost_create_response_foreign_recovery_blocks_device_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _FakeExistingDevice(
        {
            **_existing_payload(device_type_id=42, role_id=10),
            "description": "Proxmox Node pve01",
        }
    )
    target = _target(desired_name="pve01.renamed")
    target.endpoint_id = 501
    target.ownership_claim_required = True

    class _Api:
        def __init__(self) -> None:
            self.client = self
            self.sidecar: dict[str, object] | None = None
            self.posts = 0
            self.sidecar_gets = 0
            self.device_patches = 0

        async def request(
            self,
            method: str,
            path: str,
            *,
            query: dict[str, object] | None = None,
            payload: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> ApiResponse:
            if method == "GET" and path == sync_state_writer.DEVICE_SYNC_STATE_PATH:
                self.sidecar_gets += 1
                rows = [] if self.sidecar is None else [self.sidecar]
                return ApiResponse(
                    status=200,
                    text=json.dumps({"count": len(rows), "next": None, "results": rows}),
                )
            if method == "POST" and path == sync_state_writer.DEVICE_SYNC_STATE_PATH:
                self.posts += 1
                self.sidecar = {
                    "id": 901,
                    "device": {"id": 101},
                    "proxmox_endpoint_raw_id": 502,
                }
                raise RuntimeError("Server disconnected")
            if method == "PATCH" and path.startswith("/api/dcim/devices/"):
                self.device_patches += 1
            raise AssertionError((method, path, query, payload))

    async def _resolve(*_args: Any, **_kwargs: Any) -> _FakeExistingDevice:
        return existing

    async def _no_conflict(*_args: Any, **_kwargs: Any) -> None:
        return None

    api = _Api()
    netbox_rest._reset_netbox_globals()
    sync_state_writer.reset_sidecar_availability_cache()
    monkeypatch.setattr(device_ensure, "_resolve_existing_device_for_target", _resolve)
    monkeypatch.setattr(device_ensure, "_persisted_name_conflict", _no_conflict)
    monkeypatch.setattr(netbox_rest, "_resolve_netbox_max_retries", lambda: 1)
    monkeypatch.setattr(netbox_rest, "_resolve_netbox_retry_delay", lambda: 0.0)

    await device_ensure._prepare_device_targets(api, [target])

    assert api.posts == 1
    assert api.sidecar_gets == 3
    assert api.device_patches == 0
    assert target.name_conflict is True
    assert existing.saved is False
    assert existing.applied == {}


def test_device_targets_skip_only_node_with_unusable_rendered_name() -> None:
    clusters = [
        SimpleNamespace(
            name="a" * 63,
            endpoint_name="endpoint-a",
            node_device_name_template="{node}.{cluster}",
            node_list=[SimpleNamespace(name="pve01")],
        ),
        SimpleNamespace(
            name="lab",
            endpoint_name="endpoint-a",
            node_device_name_template="{node}.{cluster}",
            node_list=[SimpleNamespace(name="pve02")],
        ),
    ]
    records = {
        cluster.name: _FakePhaseRecord({"id": index, "name": cluster.name, "scope_id": 41})
        for index, cluster in enumerate(clusters, start=11)
    }

    targets = device_ensure._build_device_targets(
        clusters,
        cluster_by_name=records,
        sites={name: _FakePhaseRecord({"id": 41}) for name in records},
    )

    assert [(target.cluster_name, target.node_name, target.desired_name) for target in targets] == [
        ("lab", "pve02", "pve02.lab")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_name", "description", "desired_name", "expected_name", "preserved"),
    (
        (
            "pve01",
            "Proxmox Node pve01",
            "pve01.cluster-a.example.com",
            "pve01.cluster-a.example.com",
            False,
        ),
        (
            "pve01.cluster-a.example.com",
            "Proxmox Node pve01.cluster-a.example.com",
            "pve01",
            "pve01",
            False,
        ),
        ("hypervisor-east", "Operator managed", "pve01.cluster-a.example.com", None, True),
    ),
)
async def test_prepare_device_target_renames_only_proxbox_managed_names(
    monkeypatch: pytest.MonkeyPatch,
    current_name: str,
    description: str,
    desired_name: str,
    expected_name: str | None,
    preserved: bool,
) -> None:
    existing = _FakeExistingDevice(
        {
            "name": current_name,
            "description": description,
            "site": 41,
            "cluster": 11,
            "tags": [{"slug": "proxbox"}],
        }
    )

    async def _resolve(*_args: Any, **_kwargs: Any) -> _FakeExistingDevice:
        return existing

    monkeypatch.setattr(device_ensure, "_resolve_existing_device_for_target", _resolve)

    async def _no_occupants(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(device_ensure, "rest_list_async", _no_occupants)
    target = _target(desired_name=desired_name)

    await device_ensure._prepare_device_targets(object(), [target])

    assert existing.applied.get("name") == expected_name
    assert target.preserve_manual_name is preserved
    assert target.effective_name == (current_name if preserved else desired_name)


@pytest.mark.asyncio
async def test_prepare_device_targets_skips_only_conflicting_rendered_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _FakeExistingDevice(
        {
            "id": 101,
            "name": "pve01",
            "description": "Proxmox Node pve01",
            "site": 41,
            "cluster": 11,
            "tags": [{"slug": "proxbox"}],
        }
    )
    second = _FakeExistingDevice(
        {
            "id": 102,
            "name": "pve02",
            "description": "Proxmox Node pve02",
            "site": 41,
            "cluster": 11,
            "tags": [{"slug": "proxbox"}],
        }
    )
    occupant = _FakeExistingDevice({"id": 999, "name": "pve02.cluster-a.example.com", "site": 41})
    targets = [
        _target(desired_name="pve01.cluster-a.example.com"),
        device_ensure._DeviceTarget(
            cluster_name="cluster-a",
            node_name="pve02",
            desired_name="pve02.cluster-a.example.com",
            effective_name="pve02.cluster-a.example.com",
            desired_site_id=41,
            site_id=41,
            cluster_id=11,
        ),
    ]

    async def _resolve(_nb: object, target: device_ensure._DeviceTarget) -> _FakeExistingDevice:
        return first if target.node_name == "pve01" else second

    async def _occupants(
        _nb: object, _path: str, *, query: dict[str, object]
    ) -> list[_FakeExistingDevice]:
        if query["name"] == "pve02.cluster-a.example.com":
            return [occupant]
        return []

    monkeypatch.setattr(device_ensure, "_resolve_existing_device_for_target", _resolve)
    monkeypatch.setattr(device_ensure, "rest_list_async", _occupants)

    await device_ensure._prepare_device_targets(object(), targets)

    assert first.applied["name"] == "pve01.cluster-a.example.com"
    assert first.saved is True
    assert second.applied == {}
    assert second.saved is False
    assert targets[0].name_conflict is False
    assert targets[1].name_conflict is True


@pytest.mark.asyncio
async def test_prepare_device_targets_preflights_all_names_before_first_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _FakeExistingDevice(
        {
            "id": 101,
            "name": "pve01",
            "description": "Proxmox Node pve01",
            "site": 41,
            "tags": [{"slug": "proxbox"}],
        }
    )
    second = _FakeExistingDevice(
        {
            "id": 102,
            "name": "pve02",
            "description": "Proxmox Node pve02",
            "site": 41,
            "tags": [{"slug": "proxbox"}],
        }
    )
    targets = [
        _target(desired_name="pve01.cluster-a.example.com"),
        device_ensure._DeviceTarget(
            cluster_name="cluster-a",
            node_name="pve02",
            desired_name="pve02.cluster-a.example.com",
            effective_name="pve02.cluster-a.example.com",
            desired_site_id=41,
            site_id=41,
            cluster_id=11,
        ),
    ]

    async def _resolve(_nb: object, target: device_ensure._DeviceTarget) -> _FakeExistingDevice:
        return first if target.node_name == "pve01" else second

    calls = 0

    async def _preflight(
        _nb: object, _path: str, *, query: dict[str, object]
    ) -> list[_FakeExistingDevice]:
        nonlocal calls
        calls += 1
        assert first.saved is False
        assert second.saved is False
        return []

    monkeypatch.setattr(device_ensure, "_resolve_existing_device_for_target", _resolve)
    monkeypatch.setattr(device_ensure, "rest_list_async", _preflight)

    await device_ensure._prepare_device_targets(object(), targets)

    assert calls == 2
    assert first.saved is True
    assert second.saved is True


@pytest.mark.asyncio
async def test_prepare_device_targets_rejects_duplicate_batch_destination_before_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = {
        "pve01": _FakeExistingDevice(
            {
                "id": 101,
                "name": "pve01",
                "description": "Proxmox Node pve01",
                "site": 41,
                "tags": [{"slug": "proxbox"}],
            }
        ),
        "pve02": _FakeExistingDevice(
            {
                "id": 102,
                "name": "pve02",
                "description": "Proxmox Node pve02",
                "site": 41,
                "tags": [{"slug": "proxbox"}],
            }
        ),
    }
    targets = [
        device_ensure._DeviceTarget(
            cluster_name=f"cluster-{index}",
            node_name=node_name,
            desired_name="shared.example.com",
            effective_name="shared.example.com",
            desired_site_id=41,
            site_id=41,
            cluster_id=10 + index,
            endpoint_id=500 + index,
        )
        for index, node_name in enumerate(records, start=1)
    ]

    async def _resolve(_nb: object, target: device_ensure._DeviceTarget):
        return records[target.node_name]

    async def _no_occupants(*_args: Any, **_kwargs: Any):
        return []

    monkeypatch.setattr(device_ensure, "_resolve_existing_device_for_target", _resolve)
    monkeypatch.setattr(device_ensure, "rest_list_async", _no_occupants)

    await device_ensure._prepare_device_targets(object(), targets)

    assert [target.name_conflict for target in targets] == [True, True]
    assert all(record.saved is False for record in records.values())
    assert all(record.applied == {} for record in records.values())


@pytest.mark.asyncio
async def test_prepare_device_targets_accepts_desired_name_owned_by_same_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _FakeExistingDevice(
        {
            "id": 101,
            "name": "pve01",
            "description": "Proxmox Node pve01",
            "site": 41,
            "tags": [{"slug": "proxbox"}],
        }
    )
    target = _target()

    async def _resolve(*_args: Any, **_kwargs: Any) -> _FakeExistingDevice:
        return existing

    async def _same_device(*_args: Any, **_kwargs: Any) -> list[_FakeExistingDevice]:
        return [_FakeExistingDevice({"id": 101, "name": "pve01.cluster-a.example.com", "site": 41})]

    monkeypatch.setattr(device_ensure, "_resolve_existing_device_for_target", _resolve)
    monkeypatch.setattr(device_ensure, "rest_list_async", _same_device)

    await device_ensure._prepare_device_targets(object(), [target])

    assert target.name_conflict is False
    assert existing.applied["name"] == target.desired_name
    assert existing.saved is True


@pytest.fixture
def stub_existing_device(monkeypatch: pytest.MonkeyPatch):
    """Patch ``rest_list_async`` to return one fake existing record."""
    holder: dict[str, _FakeExistingDevice] = {}

    def _install(record: _FakeExistingDevice) -> None:
        holder["record"] = record

        async def _fake_rest_list(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [record]

        monkeypatch.setattr(device_ensure, "rest_list_async", _fake_rest_list)

    return _install


# ── existing-device branch ───────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True], ids=["create", "existing"])
async def test_ensure_device_rejects_persisted_name_conflict_before_writes(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    current = _FakeExistingDevice({"id": 101, **_existing_payload(device_type_id=42, role_id=10)})
    occupant = _FakeExistingDevice({"id": 999, "name": "pve01.cluster-a", "site": 41})
    reconcile_calls: list[None] = []
    sidecar_calls: list[None] = []

    async def _resolve(*_args: Any, **_kwargs: Any) -> _FakeExistingDevice | None:
        return current if existing else None

    async def _conflict(*_args: Any, **_kwargs: Any) -> _FakeExistingDevice:
        return occupant

    async def _reconcile(*_args: Any, **_kwargs: Any) -> object:
        reconcile_calls.append(None)
        return object()

    async def _write_sidecar(*_args: Any, **_kwargs: Any) -> None:
        sidecar_calls.append(None)

    monkeypatch.setattr(device_ensure, "_resolve_existing_device_for_target", _resolve)
    monkeypatch.setattr(device_ensure, "_persisted_name_conflict", _conflict)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _reconcile)
    monkeypatch.setattr(device_ensure, "write_device_sync_state", _write_sidecar)

    with pytest.raises(
        ProxboxException,
        match=(
            "Cannot ensure Proxmox node 'pve01': rendered device name "
            "'pve01.cluster-a' is already used in site id=41"
        ),
    ):
        await device_ensure._ensure_device(
            nb=object(),
            device_name="pve01",
            cluster_name="cluster-a",
            node_device_name_template="{node}.{cluster}",
            cluster_id=11,
            device_type_id=42,
            role_id=10,
            site_id=41,
            tag_refs=[],
        )

    assert current.saved is False
    assert current.applied == {}
    assert reconcile_calls == []
    assert sidecar_calls == []


@pytest.mark.asyncio
async def test_existing_device_default_overwrites_device_type(
    stub_existing_device,
) -> None:
    """Default flags (all True) keep historical always-overwrite behavior."""
    existing = _FakeExistingDevice(_existing_payload(device_type_id=999, role_id=10))
    stub_existing_device(existing)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve01",
        cluster_id=11,
        device_type_id=42,  # different from existing 999
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert existing.saved is True
    assert existing.applied.get("device_type") == 42


@pytest.mark.asyncio
async def test_existing_device_preserves_device_type_when_flag_disabled(
    stub_existing_device,
) -> None:
    """Regression for issue #342: ``overwrite_device_type=False`` blocks the patch."""
    existing = _FakeExistingDevice(_existing_payload(device_type_id=999, role_id=10))
    stub_existing_device(existing)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve01",
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_device_type=False,
        overwrite_flags=SyncOverwriteFlags(overwrite_device_type=False),
    )

    assert "device_type" not in existing.applied


@pytest.mark.asyncio
async def test_existing_untagged_device_different_site_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An untagged same-name node from another cluster/site is never adopted.

    Without the ``proxbox`` tag the operator has not opted in, so sync creates a
    fresh device in the target site instead of merging into the foreign record.
    """
    existing = _FakeExistingDevice(_existing_payload(device_type_id=42, role_id=10, tagged=False))
    existing._current.update({"cluster": 77, "site": 99})

    async def _fake_rest_list(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [existing]

    captured: dict[str, Any] = {}

    async def _fake_rest_reconcile(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        payload = dict(kwargs["payload"])
        payload["id"] = 101
        return _FakeExistingDevice(payload)

    monkeypatch.setattr(device_ensure, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _fake_rest_reconcile)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve01",
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert existing.saved is False
    assert existing.applied == {}
    assert captured["lookup"] == {"name": "pve01", "site_id": 41}
    assert captured["payload"]["cluster"] == 11
    assert captured["payload"]["site"] == 41


@pytest.mark.asyncio
async def test_existing_tagged_device_different_site_is_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #561: a ``proxbox``-tagged same-name device in another site is reused.

    The operator opted in by tagging the pre-existing physical hypervisor, so
    Proxbox adopts it: it is updated in place — attached to the Proxmox cluster
    and its site is moved to match the cluster's scope_site (NetBox enforces that
    device.site == cluster.scope_site, so leaving the stale site in place would
    cause a "The assigned cluster belongs to a different site" validation error).
    """
    existing = _FakeExistingDevice(_existing_payload(device_type_id=42, role_id=10, tagged=True))
    existing._current.update({"cluster": 77, "site": 99})

    async def _fake_rest_list(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [existing]

    async def _fail_rest_reconcile(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("adopted device must be patched in place, not re-created")

    monkeypatch.setattr(device_ensure, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _fail_rest_reconcile)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve01",
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_flags=SyncOverwriteFlags(),
    )

    # Existing record is updated in place: cluster and site are both moved so that
    # device.site (41) matches cluster 11's scope_site (41).  NetBox rejects any
    # PATCH that assigns a cluster whose scope_site differs from the device's site.
    assert existing.saved is True
    assert existing.applied.get("cluster") == 11
    assert existing.applied.get("site") == 41


@pytest.mark.asyncio
async def test_bulk_device_reconcile_does_not_reuse_untagged_same_name_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Untagged same-name nodes in another site must not reuse the old-site record.

    Without the ``proxbox`` opt-in tag the bulk reconcile targets the desired
    Proxmox site (41) and lets name+site lookup create a fresh record there.
    """
    captured_device_phase: dict[str, Any] = {}

    async def _fake_rest_list(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [
            _FakePhaseRecord(
                {
                    "id": 200,
                    "name": "pve01",
                    "site": 99,
                    "cluster": 77,
                    "tags": [],
                }
            )
        ]

    async def _fake_bulk_phases(*args: Any, **kwargs: Any) -> dict[str, Any]:
        phases = kwargs["phases"] if "phases" in kwargs else args[1]
        phase_names = {phase.name for phase in phases}

        if "cluster_types" in phase_names:
            return {
                "cluster_types": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 1, "name": "Cluster", "slug": "cluster"})]
                ),
                "manufacturers": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 2, "name": "Proxmox", "slug": "proxmox"})]
                ),
                "device_roles": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 3, "name": "Proxmox Node"})]
                ),
                "sites": SimpleNamespace(
                    records=[
                        _FakePhaseRecord(
                            {
                                "id": 41,
                                "name": "Proxmox Default Site - PVE-CLUSTER-01",
                                "slug": "proxmox-default-site-pve-cluster-01",
                            }
                        )
                    ]
                ),
            }

        if "clusters" in phase_names:
            return {
                "clusters": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 11, "name": "PVE-CLUSTER-01"})]
                ),
                "device_types": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 4, "model": "Proxmox Generic Device"})]
                ),
            }

        device_phase = phases[0]
        captured_device_phase["lookup_fields"] = list(device_phase.lookup_fields)
        captured_device_phase["lookup_query_field_map"] = dict(
            device_phase.lookup_query_field_map or {}
        )
        captured_device_phase["payload"] = dict(device_phase.payloads[0])
        return {
            "devices": SimpleNamespace(
                records=[_FakePhaseRecord({"id": 101, **device_phase.payloads[0]})]
            )
        }

    monkeypatch.setattr(device_ensure, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(device_ensure, "rest_bulk_reconcile_phases_async", _fake_bulk_phases)

    await device_ensure.ensure_proxmox_devices_bulk(
        object(),
        clusters_status=[
            SimpleNamespace(
                name="PVE-CLUSTER-01",
                mode="cluster",
                node_list=[SimpleNamespace(name="pve01")],
            )
        ],
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert captured_device_phase["lookup_fields"] == ["name", "site"]
    assert captured_device_phase["lookup_query_field_map"] == {"site": "site_id"}
    assert captured_device_phase["payload"]["cluster"] == 11
    assert captured_device_phase["payload"]["site"] == 41


@pytest.mark.asyncio
async def test_bulk_device_reconcile_uses_reconciled_cluster_site_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cluster scope wins over stale endpoint/default site during device writes."""

    captured_device_phase: dict[str, Any] = {}

    async def _fake_rest_list(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _fake_bulk_phases(*args: Any, **kwargs: Any) -> dict[str, Any]:
        phases = kwargs["phases"] if "phases" in kwargs else args[1]
        phase_names = {phase.name for phase in phases}

        if "cluster_types" in phase_names:
            return {
                "cluster_types": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 1, "name": "Cluster", "slug": "cluster"})]
                ),
                "manufacturers": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 2, "name": "Proxmox", "slug": "proxmox"})]
                ),
                "device_roles": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 3, "name": "Proxmox Node"})]
                ),
                "sites": SimpleNamespace(
                    records=[
                        _FakePhaseRecord(
                            {
                                "id": 41,
                                "name": "Proxmox Default Site - PVE-CLUSTER-01",
                                "slug": "proxmox-default-site-pve-cluster-01",
                            }
                        )
                    ]
                ),
            }

        if "clusters" in phase_names:
            return {
                "clusters": SimpleNamespace(
                    records=[
                        _FakePhaseRecord(
                            {
                                "id": 11,
                                "name": "PVE-CLUSTER-01",
                                "scope_type": "dcim.site",
                                "scope_id": 99,
                            }
                        )
                    ]
                ),
                "device_types": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 4, "model": "Proxmox Generic Device"})]
                ),
            }

        device_phase = phases[0]
        captured_device_phase["payload"] = dict(device_phase.payloads[0])
        return {
            "devices": SimpleNamespace(
                records=[_FakePhaseRecord({"id": 101, **device_phase.payloads[0]})]
            )
        }

    monkeypatch.setattr(device_ensure, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(device_ensure, "rest_bulk_reconcile_phases_async", _fake_bulk_phases)

    await device_ensure.ensure_proxmox_devices_bulk(
        object(),
        clusters_status=[
            SimpleNamespace(
                name="PVE-CLUSTER-01",
                mode="cluster",
                node_list=[SimpleNamespace(name="pve01")],
            )
        ],
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert captured_device_phase["payload"]["cluster"] == 11
    assert captured_device_phase["payload"]["site"] == 99


@pytest.mark.asyncio
async def test_bulk_device_reconcile_adopts_tagged_same_name_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #561: a ``proxbox``-tagged same-name node in another site is adopted.

    The bulk path pins the existing record's own site (99) so name+site lookup
    targets and updates it in place, attaching it to the Proxmox cluster instead
    of creating a duplicate in the default Proxmox site.
    """
    captured_device_phase: dict[str, Any] = {}

    async def _fake_rest_list(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [
            _FakePhaseRecord(
                {
                    "id": 200,
                    "name": "pve01",
                    "site": 99,
                    "cluster": 77,
                    "tags": [{"slug": "proxbox", "name": "Proxbox"}],
                }
            )
        ]

    async def _fake_bulk_phases(*args: Any, **kwargs: Any) -> dict[str, Any]:
        phases = kwargs["phases"] if "phases" in kwargs else args[1]
        phase_names = {phase.name for phase in phases}

        if "cluster_types" in phase_names:
            return {
                "cluster_types": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 1, "name": "Cluster", "slug": "cluster"})]
                ),
                "manufacturers": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 2, "name": "Proxmox", "slug": "proxmox"})]
                ),
                "device_roles": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 3, "name": "Proxmox Node"})]
                ),
                "sites": SimpleNamespace(
                    records=[
                        _FakePhaseRecord(
                            {
                                "id": 41,
                                "name": "Proxmox Default Site - PVE-CLUSTER-01",
                                "slug": "proxmox-default-site-pve-cluster-01",
                            }
                        )
                    ]
                ),
            }

        if "clusters" in phase_names:
            return {
                "clusters": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 11, "name": "PVE-CLUSTER-01"})]
                ),
                "device_types": SimpleNamespace(
                    records=[_FakePhaseRecord({"id": 4, "model": "Proxmox Generic Device"})]
                ),
            }

        device_phase = phases[0]
        captured_device_phase["lookup_fields"] = list(device_phase.lookup_fields)
        captured_device_phase["payload"] = dict(device_phase.payloads[0])
        return {
            "devices": SimpleNamespace(
                records=[_FakePhaseRecord({"id": 200, **device_phase.payloads[0]})]
            )
        }

    monkeypatch.setattr(device_ensure, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(device_ensure, "rest_bulk_reconcile_phases_async", _fake_bulk_phases)

    await device_ensure.ensure_proxmox_devices_bulk(
        object(),
        clusters_status=[
            SimpleNamespace(
                name="PVE-CLUSTER-01",
                mode="cluster",
                node_list=[SimpleNamespace(name="pve01")],
            )
        ],
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert captured_device_phase["lookup_fields"] == ["name", "site"]
    assert captured_device_phase["payload"]["cluster"] == 11
    assert captured_device_phase["payload"]["site"] == 99


@pytest.mark.asyncio
async def test_existing_device_preserves_role_when_flag_disabled(
    stub_existing_device,
) -> None:
    existing = _FakeExistingDevice(_existing_payload(device_type_id=42, role_id=999))
    stub_existing_device(existing)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve01",
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_device_role=False,
        overwrite_flags=SyncOverwriteFlags(overwrite_device_role=False),
    )

    assert "role" not in existing.applied


@pytest.mark.asyncio
async def test_existing_device_preserves_tags_when_flag_disabled(
    stub_existing_device,
) -> None:
    existing = _FakeExistingDevice(_existing_payload(device_type_id=42, role_id=10))
    # Existing record carries a different tag set than what the sync would push.
    existing._current["tags"] = [{"id": 8, "name": "Custom", "slug": "custom", "color": "000000"}]
    stub_existing_device(existing)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve01",
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_device_tags=False,
        overwrite_flags=SyncOverwriteFlags(overwrite_device_tags=False),
    )

    assert "tags" not in existing.applied


# ── first-create branch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_create_propagates_patchable_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no device exists, ``rest_reconcile_async`` must receive the allowlist."""
    captured: list[dict[str, Any]] = []

    async def _fake_rest_list(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _fake_reconcile(*_args: Any, **kwargs: Any) -> dict[str, object]:
        captured.append(kwargs)
        if len(captured) == 1:
            return {"id": 202, "name": "pve02", "site": 41}
        return {"id": 202, **kwargs["payload"]}

    monkeypatch.setattr(device_ensure, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _fake_reconcile)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve02",
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_device_type=False,
        overwrite_flags=SyncOverwriteFlags(overwrite_device_type=False),
    )

    assert captured[0]["patchable_fields"] == frozenset()
    allowed = captured[1].get("patchable_fields")
    assert allowed is not None
    assert "device_type" not in allowed
    assert "cluster" in allowed


# ── final bulk PATCH boundary ────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.status = 200
        self._payload = payload
        self.text = "ok"

    def json(self) -> Any:
        return self._payload


class _FakeNetBoxClient:
    def __init__(self, existing: dict[str, Any]) -> None:
        self.existing = existing
        self.patch_payloads: list[Any] = []

    async def request(self, method: str, path: str, query=None, payload=None, **_kwargs):
        if method == "GET":
            return _FakeResponse({"count": 1, "results": [self.existing], "next": None})
        if method == "PATCH":
            self.patch_payloads.append(payload)
            return _FakeResponse(payload)
        raise AssertionError(f"unexpected method: {method}")


class _FakeNetBox:
    def __init__(self, existing: dict[str, Any]) -> None:
        self.client = _FakeNetBoxClient(existing)


@pytest.mark.asyncio
async def test_bulk_device_reconcile_omits_reported_fields_when_flags_disabled() -> None:
    """Regression for issue #350's May 7 log: final PATCH must honor false flags."""
    clear_rest_get_cache()
    existing = _existing_payload(device_type_id=5, role_id=5)
    existing.update(
        {
            "id": 1,
            "cluster": {"id": 10},
            "site": {"id": 41},
            "device_type": {"id": 5, "model": "Custom Device"},
            "role": {"id": 5, "name": "Custom Role"},
            "description": "operator-owned description",
            "tags": [
                {"id": 8, "name": "AH", "slug": "ah", "color": "000000"},
                {"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"},
            ],
        }
    )
    nb = _FakeNetBox(existing)
    flags = SyncOverwriteFlags(
        overwrite_device_role=False,
        overwrite_device_type=False,
        overwrite_device_tags=False,
        overwrite_device_status=False,
        overwrite_device_description=False,
        overwrite_device_custom_fields=False,
    )
    patchable = device_ensure._compute_device_patchable_fields(
        flags,
        overwrite_device_role=flags.overwrite_device_role,
        overwrite_device_type=flags.overwrite_device_type,
        overwrite_device_tags=flags.overwrite_device_tags,
    )

    await rest_bulk_reconcile_async(
        nb,
        "/api/dcim/devices/",
        payloads=[
            {
                "name": "pve01",
                "status": "active",
                "cluster": 11,
                "device_type": 38,
                "role": 13,
                "site": 41,
                "description": "Proxmox Node pve01",
                "tags": [{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
            }
        ],
        lookup_fields=["name"],
        schema=NetBoxDeviceSyncState,
        patchable_fields=frozenset(patchable),
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "status": record.get("status"),
            "cluster": device_ensure._relation_id_or_none(record.get("cluster")),
            "device_type": device_ensure._relation_id_or_none(record.get("device_type")),
            "role": device_ensure._relation_id_or_none(record.get("role")),
            "site": device_ensure._relation_id_or_none(record.get("site")),
            "description": record.get("description"),
            "tags": record.get("tags"),
        },
    )

    assert patchable == {"name", "cluster"}
    assert nb.client.patch_payloads == [[{"id": 1, "cluster": 11}]]


def _sidecar_target() -> device_ensure._DeviceTarget:
    return device_ensure._DeviceTarget(
        cluster_name="lab",
        node_name="pve2",
        desired_name="pve2",
        effective_name="pve2",
        desired_site_id=7,
        site_id=7,
        cluster_id=3,
        endpoint_id=501,
    )


def _patch_sidecar_lookup(
    monkeypatch: pytest.MonkeyPatch,
    devices: dict[int, dict[str, object] | None],
    *,
    sidecars: list[dict[str, object]] | None = None,
) -> None:
    if sidecars is None:
        sidecars = [
            {
                "id": 900 + index,
                "device": device_id,
                "proxmox_endpoint_raw_id": 501,
                "proxmox_cluster_name": "lab",
                "proxmox_node_name": "pve2",
            }
            for index, device_id in enumerate(devices)
        ]

    async def _sidecars(
        _nb: object, _path: str, *, query: dict[str, object]
    ) -> list[dict[str, object]]:
        device_id = query.get("device_id")
        if device_id is not None:
            return [row for row in sidecars if row["device"] == device_id]
        return sidecars

    async def _device(_nb: object, _path: str, *, query: dict[str, object]):
        record = devices.get(int(query["id"]))
        return _FakeExistingDevice(record) if record is not None else None

    monkeypatch.setattr(device_ensure, "rest_list_async", _sidecars)
    monkeypatch.setattr(device_ensure, "rest_first_async", _device)


@pytest.mark.asyncio
async def test_sidecar_ignores_deleted_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sidecar_lookup(
        monkeypatch,
        {10: {"id": 10, "name": "pve2", "cluster": {"id": 3}}, 12: None},
    )
    resolved = await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())
    assert resolved is not None and resolved.get("id") == 10


@pytest.mark.asyncio
async def test_sidecar_all_deleted_devices_returns_no_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sidecar_lookup(monkeypatch, {10: None, 12: None})

    resolved = await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())

    assert resolved is None


@pytest.mark.asyncio
async def test_sidecar_does_not_adopt_a_different_device_returned_by_id_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sidecar_lookup(monkeypatch, {10: {"id": 10}})

    async def _wrong_device(*_args: object, **_kwargs: object) -> _FakeExistingDevice:
        return _FakeExistingDevice({"id": 99, "name": "foreign-device", "cluster": {"id": 3}})

    monkeypatch.setattr(device_ensure, "rest_first_async", _wrong_device)

    with pytest.raises(
        device_ensure.DeviceSyncStateLookupError,
        match="returned device id=99 for requested id=10",
    ):
        await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())


@pytest.mark.asyncio
async def test_sidecar_malformed_device_lookup_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sidecar_lookup(monkeypatch, {10: {"id": 10}})

    async def _missing_id(*_args: object, **_kwargs: object) -> _FakeExistingDevice:
        return _FakeExistingDevice({"name": "malformed"})

    monkeypatch.setattr(device_ensure, "rest_first_async", _missing_id)

    with pytest.raises(
        device_ensure.DeviceSyncStateLookupError,
        match="returned a record without a usable id",
    ):
        await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())


@pytest.mark.asyncio
async def test_sidecar_conflict_fails_when_device_lookup_ignores_id_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = {
        10: {"id": 10, "name": "stale", "cluster": {"id": 9}},
        11: {"id": 11, "name": "correct", "cluster": {"id": 3}},
    }
    _patch_sidecar_lookup(monkeypatch, devices)

    async def _ignored_filter(*_args: object, **_kwargs: object) -> _FakeExistingDevice:
        return _FakeExistingDevice(devices[10])

    monkeypatch.setattr(device_ensure, "rest_first_async", _ignored_filter)

    with pytest.raises(
        device_ensure.DeviceSyncStateLookupError,
        match="returned device id=10 for requested id=11",
    ):
        await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())


@pytest.mark.asyncio
async def test_sidecar_confirmed_absent_stale_claim_keeps_correct_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sidecar_lookup(
        monkeypatch,
        {
            10: None,
            11: {"id": 11, "name": "correct", "cluster": {"id": 3}},
        },
    )

    resolved = await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())

    assert resolved is not None and resolved.get("id") == 11


@pytest.mark.asyncio
async def test_sidecar_lone_survivor_requires_target_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sidecar_lookup(
        monkeypatch,
        {
            10: None,
            11: {"id": 11, "name": "foreign", "cluster": {"id": 9}},
        },
    )

    with pytest.raises(
        device_ensure.DeviceSyncStateLookupError,
        match="Could not corroborate device id=11",
    ):
        await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())


@pytest.mark.asyncio
async def test_sidecar_ignores_rows_whose_cluster_or_node_identity_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = {
        10: {"id": 10, "name": "pve2", "cluster": {"id": 3}},
        11: {"id": 11, "name": "wrong-cluster", "cluster": {"id": 3}},
        12: {"id": 12, "name": "wrong-node", "cluster": {"id": 3}},
        13: {"id": 13, "name": "wrong-endpoint", "cluster": {"id": 3}},
    }
    sidecars = [
        {
            "id": 900,
            "device": 10,
            "proxmox_endpoint_raw_id": 501,
            "proxmox_cluster_name": "lab",
            "proxmox_node_name": "pve2",
        },
        {
            "id": 901,
            "device": 11,
            "proxmox_endpoint_raw_id": 501,
            "proxmox_cluster_name": "retired-lab",
            "proxmox_node_name": "pve2",
        },
        {
            "id": 902,
            "device": 12,
            "proxmox_endpoint_raw_id": 501,
            "proxmox_cluster_name": "lab",
            "proxmox_node_name": "pve3",
        },
        {
            "id": 903,
            "device": 13,
            "proxmox_endpoint_raw_id": 777,
            "proxmox_cluster_name": "lab",
            "proxmox_node_name": "pve2",
        },
    ]

    _patch_sidecar_lookup(monkeypatch, devices, sidecars=sidecars)

    resolved = await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())

    assert resolved is not None and resolved.get("id") == 10


@pytest.mark.asyncio
async def test_sidecar_prefers_claimed_endpoint_over_unclaimed_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = {
        10: {"id": 10, "name": "claimed", "cluster": {"id": 9}, "site": {"id": 8}},
        11: {"id": 11, "name": "unclaimed", "cluster": {"id": 3}, "site": {"id": 7}},
    }
    sidecars = [
        {
            "id": 900,
            "device": 10,
            "proxmox_endpoint_raw_id": 501,
            "proxmox_cluster_name": "lab",
            "proxmox_node_name": "pve2",
        },
        {
            "id": 901,
            "device": 11,
            "proxmox_endpoint_raw_id": None,
            "proxmox_cluster_name": "lab",
            "proxmox_node_name": "pve2",
        },
    ]

    _patch_sidecar_lookup(monkeypatch, devices, sidecars=sidecars)

    resolved = await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())

    assert resolved is not None and resolved.get("id") == 10


@pytest.mark.asyncio
async def test_sidecar_keeps_manually_renamed_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sidecar_lookup(
        monkeypatch,
        {
            10: {"id": 10, "name": "custom-name", "cluster": {"id": 3}, "site": {"id": 7}},
            11: {"id": 11, "name": "pve2", "cluster": {"id": 9}, "site": {"id": 8}},
        },
    )
    resolved = await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())
    assert resolved is not None and resolved.get("id") == 10


@pytest.mark.asyncio
async def test_sidecar_same_site_other_cluster_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sidecar_lookup(
        monkeypatch,
        {
            10: {"id": 10, "name": "pve2", "cluster": {"id": 9}, "site": {"id": 7}},
            11: {"id": 11, "name": "pve2", "cluster": {"id": 8}, "site": {"id": 5}},
        },
    )
    with pytest.raises(ProxboxException, match="Ambiguous Proxmox node device identity"):
        await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())


@pytest.mark.asyncio
async def test_sidecar_site_match_accepted_only_for_clusterless_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sidecar_lookup(
        monkeypatch,
        {
            10: {"id": 10, "name": "pve2", "cluster": {"id": 9}, "site": {"id": 7}},
            11: {"id": 11, "name": "pve2", "site": {"id": 7}},
        },
    )
    resolved = await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())
    assert resolved is not None and resolved.get("id") == 11


@pytest.mark.asyncio
async def test_sidecar_prefers_device_in_target_placement(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sidecar_lookup(
        monkeypatch,
        {
            10: {"id": 10, "name": "pve2", "cluster": {"id": 9}, "site": {"id": 8}},
            11: {"id": 11, "name": "pve2", "cluster": {"id": 3}, "site": {"id": 7}},
        },
    )
    resolved = await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())
    assert resolved is not None and resolved.get("id") == 11


@pytest.mark.asyncio
async def test_sidecar_still_fails_when_live_devices_equally_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sidecar_lookup(
        monkeypatch,
        {
            10: {"id": 10, "name": "pve2", "cluster": {"id": 9}, "site": {"id": 8}},
            11: {"id": 11, "name": "pve2", "cluster": {"id": 9}, "site": {"id": 8}},
        },
    )
    with pytest.raises(ProxboxException, match="Ambiguous Proxmox node device identity"):
        await device_ensure._device_from_identity_sidecar(object(), _sidecar_target())


@pytest.mark.asyncio
async def test_sidecar_unset_placement_does_not_match_unset_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sidecar_lookup(
        monkeypatch,
        {10: {"id": 10, "name": "pve2"}, 11: {"id": 11, "name": "pve2"}},
    )
    target = _sidecar_target()
    target.cluster_id = None
    target.desired_site_id = None
    with pytest.raises(ProxboxException, match="Ambiguous Proxmox node device identity"):
        await device_ensure._device_from_identity_sidecar(object(), target)


@pytest.mark.asyncio
async def test_sidecar_cluster_deleted_conflict_names_rows_to_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sidecar_lookup(
        monkeypatch,
        {
            10: {"id": 10, "name": "pve2-old", "site": {"id": 7}},
            11: {"id": 11, "name": "pve2-new", "site": {"id": 7}},
        },
    )
    target = _sidecar_target()
    target.cluster_id = None

    with pytest.raises(ProxboxException) as caught:
        await device_ensure._device_from_identity_sidecar(object(), target)

    assert caught.value.message == "Ambiguous Proxmox node device identity"
    assert caught.value.detail is not None
    detail = str(caught.value.detail)
    assert "device ids [10, 11]" in detail
    assert "device sync-state row ids [900, 901]" in detail
    assert "Remove obsolete device sync-state rows" in detail
