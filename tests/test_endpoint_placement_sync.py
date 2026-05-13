"""Regression tests for endpoint site/tenant placement propagation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from proxbox_api.services.sync import device_ensure
from proxbox_api.services.sync.virtual_machines import build_netbox_virtual_machine_payload


@pytest.mark.asyncio
async def test_ensure_site_reuses_configured_endpoint_site(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups: list[tuple[str, dict[str, object]]] = []

    async def _fake_first(_nb: object, path: str, *, query: dict[str, object] | None = None):
        lookups.append((path, query or {}))
        return SimpleNamespace(id=42, name="DC 1", slug="dc1")

    async def _unexpected_reconcile(*_args: object, **_kwargs: object):
        raise AssertionError("default site reconcile should not run when a site is configured")

    monkeypatch.setattr(device_ensure, "rest_first_async", _fake_first)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _unexpected_reconcile)

    site = await device_ensure._ensure_site(
        object(),
        cluster_name="cluster-a",
        tag_refs=[],
        placement=SimpleNamespace(site_id=42, site_slug="dc1", site_name="DC 1"),
    )

    assert site.id == 42
    assert lookups == [("/api/dcim/sites/", {"id": 42, "limit": 2})]


@pytest.mark.asyncio
async def test_ensure_cluster_sets_site_scope_and_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_reconcile(*_args: object, **kwargs: object):
        captured.update(kwargs)
        return SimpleNamespace(id=77)

    async def _fake_first(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(device_ensure, "rest_first_async", _fake_first)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _fake_reconcile)

    await device_ensure._ensure_cluster(
        object(),
        cluster_name="cluster-a",
        cluster_type_id=5,
        mode="cluster",
        tag_refs=[],
        site_id=42,
        tenant_id=9,
    )

    payload = captured["payload"]
    assert payload["scope_type"] == "dcim.site"
    assert payload["scope_id"] == 42
    assert payload["tenant"] == 9
    # Issue #362: on create, the cluster discovery slug must be present.
    tag_slugs = {ref.get("slug") for ref in payload["tags"] if isinstance(ref, dict)}
    assert "proxbox-discovered-cluster" in tag_slugs


def test_vm_payload_includes_endpoint_site_and_tenant() -> None:
    payload = build_netbox_virtual_machine_payload(
        proxmox_resource={
            "vmid": 101,
            "name": "vm-101",
            "node": "pve01",
            "type": "qemu",
            "status": "running",
            "maxcpu": 2,
            "maxmem": 1024 * 1024 * 1024,
            "maxdisk": 0,
        },
        proxmox_config={},
        cluster_id=7,
        device_id=8,
        role_id=9,
        tag_ids=[10],
        site_id=42,
        tenant_id=11,
        cluster_name="cluster-a",
    )

    assert payload["site"] == 42
    assert payload["tenant"] == 11
