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

from types import SimpleNamespace
from typing import Any

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import clear_rest_get_cache, rest_bulk_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxDeviceSyncState
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync import device_ensure


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

    async def _reconcile(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {"id": 202}

    async def _write(*_args: Any, **kwargs: Any) -> None:
        writes.append(kwargs)

    monkeypatch.setattr(device_ensure, "_prepare_device_targets", _prepare)
    monkeypatch.setattr(device_ensure, "resolve_discovery_tag_id", _no_tag)
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
        endpoint_id = int(query["proxmox_endpoint_raw_id"])
        return [{"device": endpoint_id + 1000}]

    async def _device(_nb: object, _path: str, *, query: dict[str, object]) -> _FakeExistingDevice:
        return _FakeExistingDevice({"id": query["id"]})

    monkeypatch.setattr(device_ensure, "rest_list_async", _sidecars)
    monkeypatch.setattr(device_ensure, "rest_first_async", _device)

    resolved = [
        await device_ensure._device_from_identity_sidecar(object(), target) for target in targets
    ]

    assert [record.get("id") for record in resolved if record is not None] == [1501, 1502]
    assert [query["proxmox_endpoint_raw_id"] for query in queries] == [501, 502]


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
    captured: dict[str, Any] = {}

    async def _fake_rest_list(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _fake_reconcile(*_args: Any, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

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

    allowed = captured.get("patchable_fields")
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
    )


def _patch_sidecar_lookup(monkeypatch: pytest.MonkeyPatch, devices: dict[int, dict]) -> None:
    async def _sidecars(
        _nb: object, _path: str, *, query: dict[str, object]
    ) -> list[dict[str, object]]:
        return [{"device": device_id} for device_id in devices]

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
