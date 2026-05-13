"""First-discovery audit tag contract (issue #362).

The four ``proxbox-discovered-*`` slugs are stamped onto Proxbox-managed
objects **only when the reconciler takes the create branch**. The update
branch must never re-add them, and operator removal from NetBox is
permanent. These tests pin both halves of that invariant for the cluster
individual reconciler. The VM/device equivalents share the same contract
and are exercised indirectly through the discovery-tag helper module.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from proxbox_api import netbox_rest
from proxbox_api.constants import (
    DISCOVERY_TAG_CLUSTER,
    DISCOVERY_TAG_NODE,
    DISCOVERY_TAG_VM_LXC,
    DISCOVERY_TAG_VM_QEMU,
)
from proxbox_api.services.sync.discovery_tags import (
    discovery_tag_ref,
    merge_tag_refs,
    vm_discovery_tag_slug,
)
from proxbox_api.services.sync.individual.cluster_sync import sync_cluster_individual
from tests.factories.session import make_session, make_settings

_TAG = SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722")
_PROXBOX_REF = {"id": 7, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}
_FROZEN_NOW = "2026-04-29T00:00:00+00:00"


class _FakeRecord:
    """Stand-in NetBox record that records ``.save()`` calls as PATCH traffic."""

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


# --------------------------------------------------------------------------- #
# Pure helpers — no NetBox required.                                          #
# --------------------------------------------------------------------------- #


def test_discovery_tag_ref_returns_slug_only_ref() -> None:
    """``discovery_tag_ref`` builds the canonical slug-only ref form."""
    assert discovery_tag_ref(DISCOVERY_TAG_CLUSTER) == {
        "slug": "proxbox-discovered-cluster"
    }


def test_vm_discovery_slug_routes_qemu_and_lxc() -> None:
    """QEMU/LXC each get their own audit tag slug."""
    assert vm_discovery_tag_slug("qemu") == DISCOVERY_TAG_VM_QEMU
    assert vm_discovery_tag_slug("lxc") == DISCOVERY_TAG_VM_LXC
    # Unknown vm_type falls back to QEMU (matches reconciler's resource defaulting).
    assert vm_discovery_tag_slug("unknown") == DISCOVERY_TAG_VM_QEMU


def test_merge_tag_refs_preserves_discovery_on_update() -> None:
    """If the existing record already carries a discovery tag, ``merge_tag_refs``
    keeps it so a sync's tags-baseline does not strip it on update."""
    baseline = [_PROXBOX_REF]
    existing = [
        _PROXBOX_REF,
        {"id": 8, "slug": DISCOVERY_TAG_CLUSTER, "name": "Discovered Cluster"},
    ]
    merged = merge_tag_refs(baseline, existing)
    merged_slugs = {ref["slug"] for ref in merged}
    assert "proxbox" in merged_slugs
    assert DISCOVERY_TAG_CLUSTER in merged_slugs


def test_merge_tag_refs_preserves_operator_tags() -> None:
    """Operator-added tags must survive merge, not just discovery slugs."""
    merged = merge_tag_refs(
        [_PROXBOX_REF],
        [{"id": 99, "slug": "operator-tag", "name": "Operator"}],
    )
    slugs = {ref["slug"] for ref in merged}
    assert slugs == {"proxbox", "operator-tag"}


def test_merge_tag_refs_dedupes_when_baseline_wins() -> None:
    """When base and existing share a slug, the base ref is kept exactly once."""
    duplicate_existing = [_PROXBOX_REF, _PROXBOX_REF]
    merged = merge_tag_refs([_PROXBOX_REF], duplicate_existing)
    assert merged == [_PROXBOX_REF]


def test_discovery_tag_slug_inventory_covers_all_four_kinds() -> None:
    """Sanity check: constants module exposes one slug per kind."""
    assert DISCOVERY_TAG_VM_QEMU != DISCOVERY_TAG_VM_LXC
    assert DISCOVERY_TAG_CLUSTER != DISCOVERY_TAG_NODE
    assert {
        DISCOVERY_TAG_VM_QEMU,
        DISCOVERY_TAG_VM_LXC,
        DISCOVERY_TAG_CLUSTER,
        DISCOVERY_TAG_NODE,
    } == {
        "proxbox-discovered-qemu",
        "proxbox-discovered-lxc",
        "proxbox-discovered-cluster",
        "proxbox-discovered-node",
    }


# --------------------------------------------------------------------------- #
# Cluster reconciler — create vs. update contract.                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def empty_netbox(monkeypatch: pytest.MonkeyPatch):
    """NetBox with no cluster yet. Records POST traffic and asserts the
    create payload carries the discovery slug."""
    posts: list[dict[str, Any]] = []

    async def _fake_first(_nb: object, path: str, *, query: dict[str, Any]) -> Any:
        del query
        # The cluster-type lookup still needs to miss so its create branch
        # fires; the cluster pre-check returns None to force create.
        del path
        return None

    async def _fake_create(_nb: object, path: str, payload: dict[str, Any], **_kw: Any) -> Any:
        posts.append({"path": path, "payload": dict(payload)})
        return _FakeRecord(payload, record_id=42)

    async def _fake_list(_nb: object, _path: str, query: dict[str, Any] | None = None) -> list[Any]:
        del query
        return []

    monkeypatch.setattr(netbox_rest, "rest_first_async", _fake_first)
    monkeypatch.setattr(netbox_rest, "rest_create_async", _fake_create)
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.rest_list_async",
        _fake_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.rest_first_async",
        _fake_first,
    )

    import proxbox_api.services.netbox_writers as nw

    nw_original = nw._last_updated_cf
    nw._last_updated_cf = lambda: {"proxmox_last_updated": _FROZEN_NOW}

    yield posts

    nw._last_updated_cf = nw_original


@pytest.mark.asyncio
async def test_first_cluster_sync_stamps_discovery_tag(empty_netbox: list[dict[str, Any]]) -> None:
    """The very first sync into an empty NetBox must include the discovery
    slug in the cluster's create payload."""
    ctx = make_session(
        nb=object(),
        px_sessions=[SimpleNamespace(name="lab")],
        tag=_TAG,
        settings=make_settings(),
        operation_id="test-discovery-first-sync",
    )

    result = await sync_cluster_individual(ctx, "lab")
    assert result["action"] == "created", result

    cluster_posts = [
        post for post in empty_netbox if post["path"] == "/api/virtualization/clusters/"
    ]
    assert len(cluster_posts) == 1
    tags_on_create = cluster_posts[0]["payload"].get("tags") or []
    posted_slugs = {tag.get("slug") for tag in tags_on_create if isinstance(tag, dict)}
    assert DISCOVERY_TAG_CLUSTER in posted_slugs
    assert "proxbox" in posted_slugs


@pytest.fixture
def cluster_with_discovery_tag(monkeypatch: pytest.MonkeyPatch):
    """NetBox where the cluster already exists *with the discovery tag*.

    Models the post-first-sync world. The second sync must not PATCH the
    cluster (the baseline matches by-slug after merge) and must keep the
    discovery slug in place.
    """
    cluster_type_record = _FakeRecord(
        {
            "name": "Cluster",
            "slug": "cluster",
            "description": "Proxmox cluster mode",
            "tags": [_PROXBOX_REF],
            "custom_fields": {"proxmox_last_updated": _FROZEN_NOW},
        },
        record_id=7,
    )
    cluster_record = _FakeRecord(
        {
            "name": "lab",
            "type": {"id": 7, "name": "Cluster", "slug": "cluster"},
            "description": "Proxmox cluster cluster.",
            "tags": [
                _PROXBOX_REF,
                {
                    "id": 8,
                    "name": "Proxbox: Discovered Cluster",
                    "slug": DISCOVERY_TAG_CLUSTER,
                    "color": "4caf50",
                },
            ],
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

    async def _fake_create(_nb: object, path: str, payload: dict[str, Any], **_kw: Any) -> Any:
        posts.append({"path": path, "payload": dict(payload)})
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
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.rest_first_async",
        _fake_first,
    )

    import proxbox_api.services.netbox_writers as nw

    nw_original = nw._last_updated_cf
    nw._last_updated_cf = lambda: {"proxmox_last_updated": _FROZEN_NOW}

    yield {"cluster": cluster_record, "cluster_type": cluster_type_record, "posts": posts}

    nw._last_updated_cf = nw_original


@pytest.mark.asyncio
async def test_resync_does_not_strip_discovery_tag(
    cluster_with_discovery_tag: dict[str, Any],
) -> None:
    """Once stamped, the discovery slug must survive every subsequent sync —
    zero PATCH traffic and the tag stays on the record."""
    ctx = make_session(
        nb=object(),
        px_sessions=[SimpleNamespace(name="lab")],
        tag=_TAG,
        settings=make_settings(),
        operation_id="test-discovery-resync",
    )

    first = await sync_cluster_individual(ctx, "lab")
    second = await sync_cluster_individual(ctx, "lab")

    assert first["action"] == "unchanged", first
    assert second["action"] == "unchanged", second
    assert cluster_with_discovery_tag["cluster"].save_calls == 0
    assert cluster_with_discovery_tag["posts"] == []

    final_tags = cluster_with_discovery_tag["cluster"].get("tags") or []
    final_slugs = {tag.get("slug") for tag in final_tags if isinstance(tag, dict)}
    assert DISCOVERY_TAG_CLUSTER in final_slugs


@pytest.fixture
def cluster_without_discovery_tag(monkeypatch: pytest.MonkeyPatch):
    """NetBox where the cluster exists from a *previous* proxbox-api version
    that pre-dates the discovery-tag scheme. The contract: an operator-style
    "apply on create only" tag must NOT be retroactively added on resync."""
    cluster_type_record = _FakeRecord(
        {
            "name": "Cluster",
            "slug": "cluster",
            "description": "Proxmox cluster mode",
            "tags": [_PROXBOX_REF],
            "custom_fields": {"proxmox_last_updated": _FROZEN_NOW},
        },
        record_id=7,
    )
    cluster_record = _FakeRecord(
        {
            "name": "lab",
            "type": {"id": 7, "name": "Cluster", "slug": "cluster"},
            "description": "Proxmox cluster cluster.",
            "tags": [_PROXBOX_REF],
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

    async def _fake_create(_nb: object, path: str, payload: dict[str, Any], **_kw: Any) -> Any:
        posts.append({"path": path, "payload": dict(payload)})
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
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.cluster_sync.rest_first_async",
        _fake_first,
    )

    import proxbox_api.services.netbox_writers as nw

    nw_original = nw._last_updated_cf
    nw._last_updated_cf = lambda: {"proxmox_last_updated": _FROZEN_NOW}

    yield {"cluster": cluster_record, "posts": posts}

    nw._last_updated_cf = nw_original


@pytest.mark.asyncio
async def test_resync_does_not_retroactively_stamp_existing_cluster(
    cluster_without_discovery_tag: dict[str, Any],
) -> None:
    """A cluster created by an older proxbox-api that lacked discovery tags
    must NOT receive the discovery slug on resync — the audit trail is
    strictly first-import."""
    ctx = make_session(
        nb=object(),
        px_sessions=[SimpleNamespace(name="lab")],
        tag=_TAG,
        settings=make_settings(),
        operation_id="test-no-retroactive-stamp",
    )

    result = await sync_cluster_individual(ctx, "lab")
    assert result["action"] == "unchanged", result
    assert cluster_without_discovery_tag["cluster"].save_calls == 0
    assert cluster_without_discovery_tag["posts"] == []
    tags = cluster_without_discovery_tag["cluster"].get("tags") or []
    slugs = {tag.get("slug") for tag in tags if isinstance(tag, dict)}
    assert DISCOVERY_TAG_CLUSTER not in slugs
