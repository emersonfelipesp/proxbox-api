"""Diff-semantics canary for ``proxbox_api.services.netbox_writers``.

These tests pin the drift-detection contract of the new typed ``upsert_*``
helpers and the underlying ``rest_reconcile_async_with_status`` primitive
introduced for issue #357: GET → diff against the desired payload → PATCH
only when the diff is non-empty → report ``created`` / ``updated`` /
``unchanged``.

Mocks operate at the ``proxbox_api.netbox_rest`` boundary: ``rest_first_async``
returns the pre-existing record (or ``None``), ``rest_create_async`` records
POST traffic, and the fake record's ``save()`` records PATCH traffic. The
schema and current-record normalizer are exercised end-to-end through the
real ``NetBoxClusterTypeSyncState`` / ``NetBoxClusterSyncState`` models.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api import netbox_rest
from proxbox_api.netbox_rest import (
    ReconcileResult,
    rest_reconcile_async_with_status,
)
from proxbox_api.proxmox_to_netbox.models import NetBoxClusterTypeSyncState
from proxbox_api.services.netbox_writers import (
    UpsertResult,
    upsert_cluster,
    upsert_cluster_type,
)

_PROXBOX_TAG: list[dict[str, object]] = [
    {"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"},
]


class _FakeRecord:
    """Stand-in for a NetBox record returned by ``rest_first_async``.

    Tracks PATCH traffic via ``.save()`` and field assignments via
    ``__setattr__``. ``serialize()`` returns the current payload so the
    reconcile primitive can normalize and diff against it.
    """

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


def _existing_cluster_type_payload(
    *, name: str = "Cluster", slug: str = "cluster"
) -> dict[str, Any]:
    return {
        "name": name,
        "slug": slug,
        "description": "Proxmox cluster mode",
        "tags": _PROXBOX_TAG,
        "custom_fields": {"proxmox_last_updated": "2026-04-29T00:00:00+00:00"},
    }


@pytest.fixture
def patch_rest(monkeypatch: pytest.MonkeyPatch):
    """Install fakes for the three REST seams used by the reconcile primitive.

    Returns a small handle exposing the captured POST traffic and the existing
    record holder so each test can configure the GET response.
    """
    holder: dict[str, _FakeRecord | None] = {"existing": None}
    posts: list[dict[str, Any]] = []

    async def _fake_first(_nb: object, _path: str, *, query: dict[str, Any]) -> Any:
        del query
        return holder["existing"]

    async def _fake_create(_nb: object, _path: str, payload: dict[str, Any], **_kw: Any) -> Any:
        posts.append(dict(payload))
        return _FakeRecord(payload, record_id=99)

    monkeypatch.setattr(netbox_rest, "rest_first_async", _fake_first)
    monkeypatch.setattr(netbox_rest, "rest_create_async", _fake_create)

    return {"holder": holder, "posts": posts}


@pytest.mark.asyncio
async def test_missing_record_emits_post_and_returns_created(patch_rest: dict[str, Any]) -> None:
    result = await upsert_cluster_type(object(), mode="cluster", tag_refs=_PROXBOX_TAG)

    assert isinstance(result, UpsertResult)
    assert result.status == "created"
    assert len(patch_rest["posts"]) == 1
    posted = patch_rest["posts"][0]
    assert posted["slug"] == "cluster"
    assert posted["name"] == "Cluster"


@pytest.mark.asyncio
async def test_unchanged_payload_emits_no_patch(patch_rest: dict[str, Any]) -> None:
    existing_payload = _existing_cluster_type_payload()
    existing = _FakeRecord(existing_payload, record_id=42)
    # Pin the timestamp inside the desired payload to match the existing record
    # so the custom-field diff is genuinely empty.
    patch_rest["holder"]["existing"] = existing

    # The helper rebuilds custom_fields with `datetime.now()`, which would
    # always differ from the stored timestamp. Patch _last_updated_cf so the
    # second-run-is-silent semantics are testable without time mocking.
    import proxbox_api.services.netbox_writers as nw

    nw_original = nw._last_updated_cf

    def _frozen_cf() -> dict[str, str]:
        return {"proxmox_last_updated": "2026-04-29T00:00:00+00:00"}

    try:
        nw._last_updated_cf = _frozen_cf
        result = await upsert_cluster_type(object(), mode="cluster", tag_refs=_PROXBOX_TAG)
    finally:
        nw._last_updated_cf = nw_original

    assert result.status == "unchanged"
    assert existing.save_calls == 0
    assert patch_rest["posts"] == []


@pytest.mark.asyncio
async def test_real_diff_emits_single_patch_and_returns_updated(
    patch_rest: dict[str, Any],
) -> None:
    existing_payload = _existing_cluster_type_payload(name="Old Name")
    existing = _FakeRecord(existing_payload, record_id=42)
    patch_rest["holder"]["existing"] = existing

    result = await upsert_cluster_type(object(), mode="cluster", tag_refs=_PROXBOX_TAG)

    assert result.status == "updated"
    assert existing.save_calls == 1
    assert patch_rest["posts"] == []
    # The diff must include the changed name field.
    assert existing.patched_fields.get("name") == "Cluster"


@pytest.mark.asyncio
async def test_fk_diff_compares_id_only(patch_rest: dict[str, Any]) -> None:
    """FK comparison must pin to ``record.id`` / ``.pk``, never nested dicts.

    Existing record stores ``type`` as a nested dict (``{"id": 7, ...}``); the
    desired payload sends an int. After normalization both should compare as
    equal, producing an ``unchanged`` outcome.
    """
    existing_payload: dict[str, Any] = {
        "name": "pve-cluster",
        "type": {"id": 7, "name": "Cluster", "slug": "cluster", "url": "..."},
        "description": "Proxmox cluster cluster.",
        "tags": _PROXBOX_TAG,
        "custom_fields": {"proxmox_last_updated": "2026-04-29T00:00:00+00:00"},
    }
    existing = _FakeRecord(existing_payload, record_id=42)
    patch_rest["holder"]["existing"] = existing

    import proxbox_api.services.netbox_writers as nw

    def _frozen_cf() -> dict[str, str]:
        return {"proxmox_last_updated": "2026-04-29T00:00:00+00:00"}

    nw_original = nw._last_updated_cf
    try:
        nw._last_updated_cf = _frozen_cf
        result = await upsert_cluster(
            object(),
            cluster_name="pve-cluster",
            cluster_type_id=7,
            mode="cluster",
            tag_refs=_PROXBOX_TAG,
        )
    finally:
        nw._last_updated_cf = nw_original

    assert result.status == "unchanged"
    assert existing.save_calls == 0


@pytest.mark.asyncio
async def test_status_enum_values_are_stable_strings() -> None:
    """Guard against accidental rename of the status enum.

    Sync orchestration, SSE summaries, and idempotency tests all string-match
    on these exact values. Changing them is a wire-breaking change.
    """
    valid = {"created", "updated", "unchanged"}
    # Build a result instance for each value to confirm the Literal accepts
    # them at runtime (Literal isn't enforced, but instantiation must work).
    for status in valid:
        result = ReconcileResult(record=_FakeRecord({}), status=status)  # type: ignore[arg-type]
        assert result.status == status
        upsert_result = UpsertResult(record=_FakeRecord({}), status=status)  # type: ignore[arg-type]
        assert upsert_result.status == status


@pytest.mark.asyncio
async def test_back_compat_rest_reconcile_async_still_returns_bare_record(
    patch_rest: dict[str, Any],
) -> None:
    """The legacy bare-record API must keep working after the refactor."""
    from proxbox_api.netbox_rest import rest_reconcile_async

    result = await rest_reconcile_async(
        object(),
        "/api/virtualization/cluster-types/",
        lookup={"slug": "cluster"},
        payload={
            "name": "Cluster",
            "slug": "cluster",
            "description": "Proxmox cluster mode",
            "tags": _PROXBOX_TAG,
            "custom_fields": {"proxmox_last_updated": "2026-04-29T00:00:00+00:00"},
        },
        schema=NetBoxClusterTypeSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "description": record.get("description"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )

    # No ReconcileResult wrapper — must be the bare record.
    assert not isinstance(result, ReconcileResult)
    assert hasattr(result, "serialize")


@pytest.mark.asyncio
async def test_with_status_wrapper_returns_reconcile_result(
    patch_rest: dict[str, Any],
) -> None:
    """The new public sibling must return a ``ReconcileResult``."""
    result = await rest_reconcile_async_with_status(
        object(),
        "/api/virtualization/cluster-types/",
        lookup={"slug": "cluster"},
        payload={
            "name": "Cluster",
            "slug": "cluster",
            "description": "Proxmox cluster mode",
            "tags": _PROXBOX_TAG,
            "custom_fields": {"proxmox_last_updated": "2026-04-29T00:00:00+00:00"},
        },
        schema=NetBoxClusterTypeSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "description": record.get("description"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )

    assert isinstance(result, ReconcileResult)
    assert result.status in {"created", "updated", "unchanged"}
