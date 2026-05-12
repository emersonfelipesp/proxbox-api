"""Second-run-is-silent assertion for the cluster individual sync.

Issue #357 PR 1: migrating ``sync_cluster_individual`` from a heuristic
``action = "updated" if existing else "created"`` to the real
``upsert_cluster_type`` / ``upsert_cluster`` status. This test pins the
roadmap's "zero ObjectChange" criterion by proxy: when nothing has drifted,
the second invocation must report ``unchanged`` for both the cluster type and
the cluster, and must not emit any PATCH or POST traffic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from proxbox_api import netbox_rest
from proxbox_api.services.sync.individual.cluster_sync import sync_cluster_individual


class _FakeRecord:
    """Stand-in NetBox record. Tracks ``.save()`` calls as PATCH traffic."""

    def __init__(self, payload: dict[str, Any], record_id: int = 1) -> None:
        object.__setattr__(self, "_payload", dict(payload))
        object.__setattr__(self, "id", record_id)
        object.__setattr__(self, "save_calls", 0)

    def serialize(self) -> dict[str, Any]:
        return {**self._payload, "id": self.id}

    def get(self, key: str, default: Any = None) -> Any:
        return self._payload.get(key, default)

    async def save(self) -> None:
        object.__setattr__(self, "save_calls", self.save_calls + 1)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in {"_payload", "id", "save_calls"}:
            object.__setattr__(self, key, value)
            return
        self._payload[key] = value


_TAG = SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722")
_TAG_PAYLOAD = [{"id": 7, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}]
_FROZEN_NOW = "2026-04-29T00:00:00+00:00"


@pytest.fixture
def in_sync_netbox(monkeypatch: pytest.MonkeyPatch):
    """Return a NetBox stand-in whose stored cluster type and cluster already
    match the payloads that ``upsert_*`` will compute, so the second sync run
    must be a no-op end-to-end."""
    cluster_type_record = _FakeRecord(
        {
            "name": "Cluster",
            "slug": "cluster",
            "description": "Proxmox cluster mode",
            "tags": _TAG_PAYLOAD,
            "custom_fields": {"proxmox_last_updated": _FROZEN_NOW},
        },
        record_id=7,
    )
    cluster_record = _FakeRecord(
        {
            "name": "lab",
            "type": {"id": 7, "name": "Cluster", "slug": "cluster"},
            "description": "Proxmox cluster cluster.",
            "tags": _TAG_PAYLOAD,
            "custom_fields": {"proxmox_last_updated": _FROZEN_NOW},
        },
        record_id=42,
    )

    posts: list[dict[str, Any]] = []
    by_path: dict[str, _FakeRecord] = {
        "/api/virtualization/cluster-types/": cluster_type_record,
        "/api/virtualization/clusters/": cluster_record,
    }

    async def _fake_first(_nb: object, path: str, *, query: dict[str, Any]) -> Any:
        del query
        return by_path.get(path)

    async def _fake_create(_nb: object, _path: str, payload: dict[str, Any], **_kw: Any) -> Any:
        posts.append(dict(payload))
        return _FakeRecord(payload, record_id=99)

    async def _fake_list(_nb: object, _path: str, query: dict[str, Any] | None = None) -> list[Any]:
        del query
        return []

    monkeypatch.setattr(netbox_rest, "rest_first_async", _fake_first)
    monkeypatch.setattr(netbox_rest, "rest_create_async", _fake_create)
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.rest_list_async",
        _fake_list,
    )

    # Pin the helper's timestamp so the desired and stored custom_fields
    # compare equal across runs.
    import proxbox_api.services.netbox_writers as nw

    nw_original = nw._last_updated_cf
    nw._last_updated_cf = lambda: {"proxmox_last_updated": _FROZEN_NOW}

    yield {
        "cluster_type": cluster_type_record,
        "cluster": cluster_record,
        "posts": posts,
    }

    nw._last_updated_cf = nw_original


@pytest.mark.asyncio
async def test_second_cluster_sync_is_silent(in_sync_netbox: dict[str, Any]) -> None:
    """First-and-second-run idempotency: no PATCH, no POST, status unchanged."""
    first = await sync_cluster_individual(
        nb=object(),
        px=SimpleNamespace(name="lab"),
        tag=_TAG,
        cluster_name="lab",
    )
    second = await sync_cluster_individual(
        nb=object(),
        px=SimpleNamespace(name="lab"),
        tag=_TAG,
        cluster_name="lab",
    )

    assert first["action"] == "unchanged", first
    assert second["action"] == "unchanged", second
    assert {
        "object_type": "cluster_type",
        "action": "unchanged",
    } in second["dependencies_synced"]

    # Zero PATCH traffic (no .save() on the existing records).
    assert in_sync_netbox["cluster_type"].save_calls == 0
    assert in_sync_netbox["cluster"].save_calls == 0
    # Zero POST traffic.
    assert in_sync_netbox["posts"] == []
