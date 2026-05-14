"""Tests for the NetBox-side bootstrap orchestrator (issue #358).

Exercises:

- The new ``upsert_*`` helpers in
  :mod:`proxbox_api.services.netbox_writers` for the create-on-miss,
  patch-on-diff, and no-op-on-match paths.
- :func:`proxbox_api.services.netbox_bootstrap.run_netbox_bootstrap` for the
  full eager pass on an empty NetBox, second-run idempotency, the
  ``ensure_netbox_objects=False`` short-circuit, and per-entry failure
  capture into :attr:`BootstrapStatus.warnings`.

Mocks operate at the ``proxbox_api.netbox_rest`` boundary using the same
``_FakeRecord`` / ``patch_rest`` pattern as ``tests/test_netbox_writers.py``.
Timestamps are frozen via ``_last_updated_cf`` so the second-run-is-silent
assertion is robust without time-mocking the clock.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api import netbox_rest
from proxbox_api.services import netbox_bootstrap
from proxbox_api.services import netbox_writers as nw
from proxbox_api.services.netbox_bootstrap import (
    BootstrapStatus,
    run_netbox_bootstrap,
)
from proxbox_api.services.netbox_writers import (
    UpsertResult,
    upsert_custom_field,
    upsert_device_role,
    upsert_manufacturer,
    upsert_tag,
    upsert_vm_type,
)

_FROZEN_TS = "2026-04-29T00:00:00+00:00"


class _FakeRecord:
    """Stand-in for a NetBox record returned by ``rest_first_async``."""

    def __init__(self, payload: dict[str, Any], record_id: int = 1) -> None:
        object.__setattr__(self, "_payload", dict(payload))
        object.__setattr__(self, "id", record_id)
        object.__setattr__(self, "save_calls", 0)
        object.__setattr__(self, "patched_fields", {})

    def serialize(self) -> dict[str, Any]:
        return {**self._payload, "id": self.id}

    def get(self, key: str, default: Any = None) -> Any:
        return self._payload.get(key, default)

    async def save(self) -> None:
        object.__setattr__(self, "save_calls", self.save_calls + 1)
        self._payload.update(self.patched_fields)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in {"_payload", "id", "save_calls", "patched_fields"}:
            object.__setattr__(self, key, value)
            return
        self.patched_fields[key] = value


@pytest.fixture
def freeze_last_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the ``proxmox_last_updated`` custom-field value used by writers."""

    def _frozen() -> dict[str, str]:
        return {"proxmox_last_updated": _FROZEN_TS}

    monkeypatch.setattr(nw, "_last_updated_cf", _frozen)


@pytest.fixture
def patch_rest(monkeypatch: pytest.MonkeyPatch):
    """Install fakes for the three REST seams used by the reconcile primitive.

    ``records`` maps (path, lookup-tuple) → ``_FakeRecord``. Tests can install
    pre-existing rows there to exercise the patch / no-op branches.
    """
    posts: list[dict[str, Any]] = []
    records: dict[tuple[str, tuple[tuple[str, Any], ...]], _FakeRecord] = {}
    next_id = {"value": 1000}

    async def _fake_first(_nb: object, path: str, *, query: dict[str, Any]) -> _FakeRecord | None:
        filtered = {k: v for k, v in query.items() if k != "limit"}
        key = (path, tuple(sorted(filtered.items())))
        return records.get(key)

    async def _fake_create(
        _nb: object, path: str, payload: dict[str, Any], **_kw: Any
    ) -> _FakeRecord:
        posts.append({"path": path, "payload": dict(payload)})
        record_id = next_id["value"]
        next_id["value"] += 1
        return _FakeRecord(payload, record_id=record_id)

    monkeypatch.setattr(netbox_rest, "rest_first_async", _fake_first)
    monkeypatch.setattr(netbox_rest, "rest_create_async", _fake_create)

    return {"posts": posts, "records": records}


@pytest.fixture
def patch_netbox_version(monkeypatch: pytest.MonkeyPatch):
    """Force a 4.6 NetBox version so VirtualMachineType bootstrap runs."""

    async def _fake_detect(_nb: object) -> tuple[int, int, int]:
        return (4, 6, 0)

    monkeypatch.setattr(netbox_bootstrap, "detect_netbox_version", _fake_detect)


# ---------------------------------------------------------------------------
# Per-helper coverage for the new upsert_* writers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_manufacturer_creates_on_miss(
    patch_rest: dict[str, Any], freeze_last_updated: None
) -> None:
    result = await upsert_manufacturer(object(), name="Proxmox", slug="proxmox")

    assert isinstance(result, UpsertResult)
    assert result.status == "created"
    assert len(patch_rest["posts"]) == 1
    posted = patch_rest["posts"][0]["payload"]
    assert posted["name"] == "Proxmox"
    assert posted["slug"] == "proxmox"


@pytest.mark.asyncio
async def test_upsert_manufacturer_unchanged_on_match(
    patch_rest: dict[str, Any], freeze_last_updated: None
) -> None:
    existing = _FakeRecord(
        {
            "name": "Proxmox",
            "slug": "proxmox",
            "tags": [],
            "custom_fields": {"proxmox_last_updated": _FROZEN_TS},
        },
        record_id=42,
    )
    patch_rest["records"][("/api/dcim/manufacturers/", (("slug", "proxmox"),))] = existing

    result = await upsert_manufacturer(object(), name="Proxmox", slug="proxmox")

    assert result.status == "unchanged"
    assert existing.save_calls == 0
    assert patch_rest["posts"] == []


@pytest.mark.asyncio
async def test_upsert_device_role_patches_on_diff(
    patch_rest: dict[str, Any], freeze_last_updated: None
) -> None:
    existing = _FakeRecord(
        {
            "name": "OLD",
            "slug": "proxmox-node",
            "color": "00bcd4",
            "description": "Proxmox node",
            "vm_role": False,
            "tags": [],
            "custom_fields": {"proxmox_last_updated": _FROZEN_TS},
        },
        record_id=7,
    )
    patch_rest["records"][("/api/dcim/device-roles/", (("slug", "proxmox-node"),))] = existing

    result = await upsert_device_role(
        object(),
        name="Proxmox Node",
        slug="proxmox-node",
        color="00bcd4",
        description="Proxmox node",
    )

    assert result.status == "updated"
    assert existing.save_calls == 1
    assert existing.patched_fields.get("name") == "Proxmox Node"
    assert patch_rest["posts"] == []


@pytest.mark.asyncio
async def test_upsert_tag_creates_on_miss(
    patch_rest: dict[str, Any], freeze_last_updated: None
) -> None:
    result = await upsert_tag(
        object(),
        name="Proxbox",
        slug="proxbox",
        color="ff5722",
        description="Proxbox identifier",
    )

    assert result.status == "created"
    posted = patch_rest["posts"][0]["payload"]
    assert posted["slug"] == "proxbox"
    assert posted["color"] == "ff5722"


@pytest.mark.asyncio
async def test_upsert_custom_field_creates_on_miss(
    patch_rest: dict[str, Any], freeze_last_updated: None
) -> None:
    result = await upsert_custom_field(
        object(),
        name="proxbox_last_run_id",
        type="text",
        label="Last Run ID",
        object_types=[
            "dcim.device",
            "virtualization.cluster",
            "virtualization.virtualmachine",
        ],
        description="UUID of the most recent Proxbox sync run.",
        group_name="Proxbox",
    )

    assert result.status == "created"
    posted = patch_rest["posts"][0]["payload"]
    assert posted["name"] == "proxbox_last_run_id"
    assert posted["type"] == "text"
    # object_types must be sorted-deduped per the helper contract
    assert posted["object_types"] == sorted(
        {
            "dcim.device",
            "virtualization.cluster",
            "virtualization.virtualmachine",
        }
    )


@pytest.mark.asyncio
async def test_upsert_vm_type_creates_on_miss(
    patch_rest: dict[str, Any], freeze_last_updated: None
) -> None:
    result = await upsert_vm_type(
        object(),
        name="QEMU Virtual Machine",
        slug="qemu-virtual-machine",
        description="Proxmox QEMU/KVM Virtual Machine",
    )

    assert result.status == "created"
    posted = patch_rest["posts"][0]["payload"]
    assert posted["slug"] == "qemu-virtual-machine"
    assert posted["name"] == "QEMU Virtual Machine"


# ---------------------------------------------------------------------------
# Orchestrator: ``run_netbox_bootstrap``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_netbox_bootstrap_skips_when_disabled() -> None:
    status = await run_netbox_bootstrap(object(), enabled=False)

    assert isinstance(status, BootstrapStatus)
    assert status.ok is True
    assert status.skipped is True
    assert status.reason == "ensure_netbox_objects=false"
    assert status.created == []
    assert status.patched == []
    assert status.unchanged == []
    assert status.warnings == []


@pytest.mark.asyncio
async def test_run_netbox_bootstrap_creates_full_inventory_on_empty_netbox(
    patch_rest: dict[str, Any],
    freeze_last_updated: None,
    patch_netbox_version: None,
) -> None:
    status = await run_netbox_bootstrap(object())

    assert status.ok is True
    assert status.skipped is False
    assert status.warnings == []
    assert status.patched == []
    assert status.unchanged == []

    # 1 Proxbox tag + 4 discovery tags + 1 cluster_type + 1 manufacturer +
    # 1 device_type + 1 device_role + 3 VM roles + 2 VM types + 8 custom fields
    # = 22 created objects on a fresh NetBox 4.6 install.
    assert len(status.created) == 22

    # Key entries from each category must appear in the created list.
    labels = set(status.created)
    assert "tag:Proxbox" in labels
    assert "tag:proxbox-discovered-qemu" in labels
    assert "tag:proxbox-discovered-lxc" in labels
    assert "tag:proxbox-discovered-cluster" in labels
    assert "tag:proxbox-discovered-node" in labels
    assert "cluster_type:proxmox" in labels
    assert "manufacturer:proxmox" in labels
    assert "device_type:proxmox-generic-device" in labels
    assert "device_role:proxmox-node" in labels
    assert "vm_role:virtual-machine-qemu" in labels
    assert "vm_role:container-lxc" in labels
    assert "vm_role:unknown" in labels
    assert "vm_type:qemu-virtual-machine" in labels
    assert "vm_type:lxc-container" in labels
    assert "custom_field:proxmox_vm_id" in labels
    assert "custom_field:proxbox_last_run_id" in labels
    assert "custom_field:proxmox_last_updated" in labels


@pytest.mark.asyncio
async def test_run_netbox_bootstrap_second_run_is_noop(
    patch_rest: dict[str, Any],
    freeze_last_updated: None,
    patch_netbox_version: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the first eager pass, a second invocation must not create or patch.

    The fake create handler stashes the posted payload under the lookup the
    next GET will use, so the second pass finds an exact match for every
    object and reports ``unchanged``.
    """
    # Map a posted payload back to the lookup the helper will use on its
    # subsequent GET. Each helper's lookup key is well-known per path.
    path_to_lookup_field: dict[str, str] = {
        "/api/extras/tags/": "slug",
        "/api/virtualization/cluster-types/": "slug",
        "/api/dcim/manufacturers/": "slug",
        "/api/dcim/device-types/": "model",
        "/api/dcim/device-roles/": "slug",
        "/api/virtualization/virtual-machine-types/": "slug",
        "/api/extras/custom-fields/": "name",
    }

    posts = patch_rest["posts"]
    records = patch_rest["records"]
    next_id = {"value": 5000}

    async def _fake_first(_nb: object, path: str, *, query: dict[str, Any]) -> _FakeRecord | None:
        filtered = {k: v for k, v in query.items() if k != "limit"}
        key = (path, tuple(sorted(filtered.items())))
        return records.get(key)

    async def _fake_create(
        _nb: object, path: str, payload: dict[str, Any], **_kw: Any
    ) -> _FakeRecord:
        posts.append({"path": path, "payload": dict(payload)})
        record_id = next_id["value"]
        next_id["value"] += 1
        record = _FakeRecord(payload, record_id=record_id)
        # Register the created record under the lookup the next bootstrap
        # pass will issue. Tags, slugs, and names all hash to a 1-tuple.
        lookup_field = path_to_lookup_field.get(path)
        if lookup_field is not None and lookup_field in payload:
            lookup_key = (path, ((lookup_field, payload[lookup_field]),))
            records[lookup_key] = record
        return record

    monkeypatch.setattr(netbox_rest, "rest_first_async", _fake_first)
    monkeypatch.setattr(netbox_rest, "rest_create_async", _fake_create)

    first = await run_netbox_bootstrap(object())
    assert first.created and not first.patched and not first.warnings

    # Reset POST tracking so the second-pass assertion is clean.
    posts.clear()

    second = await run_netbox_bootstrap(object())

    assert second.ok is True
    assert second.skipped is False
    assert second.warnings == []
    assert second.created == [], f"second pass should not create anything, got: {second.created}"
    assert second.patched == [], f"second pass should not patch anything, got: {second.patched}"
    assert len(second.unchanged) == len(first.created)
    assert posts == [], "no POSTs may be issued on the second pass"


@pytest.mark.asyncio
async def test_run_netbox_bootstrap_captures_per_entry_failures(
    patch_rest: dict[str, Any],
    freeze_last_updated: None,
    patch_netbox_version: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403-style failure on a single tag must not abort the orchestrator.

    We let the first ``upsert_tag`` call fail and assert that the run
    continues, recording the failure in ``BootstrapStatus.warnings`` while
    still processing the remaining objects.
    """
    original_first = netbox_rest.rest_first_async
    original_create = netbox_rest.rest_create_async
    raise_once = {"done": False}

    async def _fake_first(*args: Any, **kwargs: Any) -> Any:
        path = args[1] if len(args) >= 2 else kwargs.get("path")
        if (
            path == "/api/extras/tags/"
            and not raise_once["done"]
            and kwargs.get("query", {}).get("slug") == "proxbox"
        ):
            raise_once["done"] = True
            raise RuntimeError("403 forbidden: cannot read tags")
        return await original_first(*args, **kwargs)

    monkeypatch.setattr(netbox_rest, "rest_first_async", _fake_first)
    monkeypatch.setattr(netbox_rest, "rest_create_async", original_create)

    status = await run_netbox_bootstrap(object())

    assert status.ok is False  # warnings flip ok to False
    assert status.skipped is False
    assert len(status.warnings) == 1
    warning = status.warnings[0]
    assert warning["object"] == "tag:Proxbox"
    assert "403" in warning["error"]
    # The remaining 21 inventory entries must still have been created.
    assert len(status.created) == 21
    assert "tag:proxbox-discovered-qemu" in status.created
    assert "tag:proxbox-discovered-lxc" in status.created
    assert "cluster_type:proxmox" in status.created
    assert "custom_field:proxbox_last_run_id" in status.created


@pytest.mark.asyncio
async def test_bootstrap_status_as_dict_round_trip() -> None:
    status = BootstrapStatus(
        ok=False,
        skipped=False,
        reason="bootstrap_error: boom",
        warnings=[{"object": "tag:Proxbox", "error": "boom"}],
        created=["manufacturer:proxmox"],
        patched=[],
        unchanged=["cluster_type:proxmox"],
    )

    payload = status.as_dict()

    assert payload == {
        "ok": False,
        "skipped": False,
        "reason": "bootstrap_error: boom",
        "warnings": [{"object": "tag:Proxbox", "error": "boom"}],
        "created": ["manufacturer:proxmox"],
        "patched": [],
        "unchanged": ["cluster_type:proxmox"],
    }
    # Lists must be defensive copies so callers can't mutate the dataclass.
    payload["created"].append("evil")
    assert status.created == ["manufacturer:proxmox"]


@pytest.mark.asyncio
async def test_run_netbox_bootstrap_skips_vm_types_on_old_netbox(
    patch_rest: dict[str, Any],
    freeze_last_updated: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NetBox < 4.6 must skip VirtualMachineType bootstrap.

    Capability gating is the contract: writing to the unsupported endpoint
    would 404 against a real NetBox 4.5 install.
    """

    async def _fake_detect_old(_nb: object) -> tuple[int, int, int]:
        return (4, 5, 8)

    monkeypatch.setattr(netbox_bootstrap, "detect_netbox_version", _fake_detect_old)

    status = await run_netbox_bootstrap(object())

    assert status.ok is True
    assert not any(label.startswith("vm_type:") for label in status.created)
    # The other 20 inventory entries must still be created.
    assert len(status.created) == 20
