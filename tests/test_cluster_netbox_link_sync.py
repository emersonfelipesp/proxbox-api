"""Regression tests for ProxmoxCluster to NetBox cluster linking."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from proxbox_api.services.netbox_writers import UpsertResult
from proxbox_api.services.sync.cluster_links import PROXMOX_CLUSTER_PATH
from proxbox_api.services.sync.individual.cluster_sync import sync_cluster_individual
from tests.factories.session import make_session, make_settings


class _FakeRecord:
    def __init__(self, payload: dict[str, Any], record_id: int) -> None:
        self._payload = dict(payload)
        self.id = record_id

    def serialize(self) -> dict[str, Any]:
        return {**self._payload, "id": self.id}

    def get(self, key: str, default: Any = None) -> Any:
        return self._payload.get(key, default)


_TAG = SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722")


def _ctx() -> Any:
    return make_session(
        nb=object(),
        px_sessions=[SimpleNamespace(name="lab")],
        tag=_TAG,
        settings=make_settings(),
        operation_id="test-cluster-netbox-link",
    )


def _patch_cluster_upserts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cluster_status: str,
) -> None:
    async def _fake_rest_first_async(_nb: object, _path: str, *, query: dict[str, Any]) -> Any:
        assert query["name"] == "lab"
        return _FakeRecord(
            {
                "name": "lab",
                "type": {"id": 7, "name": "Cluster", "slug": "cluster"},
                "tags": [{"id": 7, "name": "Proxbox", "slug": "proxbox"}],
            },
            record_id=42,
        )

    async def _fake_upsert_cluster_type(
        _nb: object,
        *,
        mode: str,
        tag_refs: list[dict[str, object]],
    ) -> UpsertResult:
        assert mode == "cluster"
        assert tag_refs
        return UpsertResult(record=_FakeRecord({"id": 7}, record_id=7), status="unchanged")

    async def _fake_upsert_cluster(_nb: object, **kwargs: Any) -> UpsertResult:
        assert kwargs["cluster_name"] == "lab"
        return UpsertResult(
            record=_FakeRecord({"id": 42, "name": "lab"}, record_id=42),
            status=cluster_status,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.rest_first_async",
        _fake_rest_first_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.upsert_cluster_type",
        _fake_upsert_cluster_type,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.upsert_cluster",
        _fake_upsert_cluster,
    )


def _patch_cluster_link(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[_FakeRecord],
) -> list[dict[str, Any]]:
    patches: list[dict[str, Any]] = []

    async def _fake_resolve(_nb: object, cluster_name: str, *, cache=None) -> int | None:
        del cache
        assert cluster_name == "lab"
        return 42

    async def _fake_list(_nb: object, path: str, *, query: dict[str, Any] | None = None):
        assert path == PROXMOX_CLUSTER_PATH
        assert query == {"name": "lab"}
        return rows

    async def _fake_patch(
        _nb: object,
        path: str,
        record_id: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        assert path == PROXMOX_CLUSTER_PATH
        patches.append({"record_id": record_id, "payload": dict(payload)})
        return {"id": record_id, **payload}

    monkeypatch.setattr(
        "proxbox_api.services.sync.cluster_links.resolve_netbox_cluster_id_by_name",
        _fake_resolve,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.cluster_links.rest_list_async",
        _fake_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.cluster_links.rest_patch_async",
        _fake_patch,
    )
    return patches


@pytest.mark.asyncio
async def test_cluster_sync_sets_matching_proxmox_cluster_netbox_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cluster_upserts(monkeypatch, cluster_status="created")
    patches = _patch_cluster_link(
        monkeypatch,
        [_FakeRecord({"name": "lab", "netbox_cluster": None}, record_id=90)],
    )

    result = await sync_cluster_individual(_ctx(), "lab")

    assert result["action"] == "created"
    assert patches == [{"record_id": 90, "payload": {"netbox_cluster": 42}}]


@pytest.mark.asyncio
async def test_cluster_resync_backfills_all_matching_proxmox_cluster_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cluster_upserts(monkeypatch, cluster_status="unchanged")
    patches = _patch_cluster_link(
        monkeypatch,
        [
            _FakeRecord(
                {
                    "name": "lab",
                    "endpoint": {"id": 10, "name": "pve01"},
                    "netbox_cluster": None,
                },
                record_id=90,
            ),
            _FakeRecord(
                {
                    "name": "lab",
                    "endpoint": {"id": 11, "name": "pve02"},
                    "netbox_cluster": None,
                },
                record_id=91,
            ),
        ],
    )

    result = await sync_cluster_individual(_ctx(), "lab")

    assert result["action"] == "unchanged"
    assert patches == [
        {"record_id": 90, "payload": {"netbox_cluster": 42}},
        {"record_id": 91, "payload": {"netbox_cluster": 42}},
    ]
