"""Regression tests for cross-endpoint Proxmox VMID collisions.

When the same Proxmox VM ID (vmid) exists on two standalone endpoints, NetBox
VM resolution must be scoped by ``(proxmox_endpoint_id, vmid)``. Keying on vmid
alone, or on a fragile cluster relation, mapped interfaces/IPs to the wrong
VirtualMachine and dropped duplicate vmids.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    _build_vm_index_by_proxmox_id,
    _resolve_netbox_virtual_machine_by_proxmox_id,
)
from proxbox_api.services.sync.individual.helpers import ensure_vm_record
from proxbox_api.services.sync.vm_helpers import resolve_netbox_cluster_id_by_name

# Two clusters share vmid=100. Each owns one distinct NetBox VM record.
CLUSTER_ALPHA_ID = 11
CLUSTER_BETA_ID = 22
ENDPOINT_ALPHA_ID = 101
ENDPOINT_BETA_ID = 202
SHARED_VMID = 100

VM_IN_ALPHA = {
    "id": 1001,
    "name": "shared-vm-alpha",
    "cluster": {"id": CLUSTER_ALPHA_ID, "name": "alpha"},
    "custom_fields": {
        "proxmox_endpoint_id": ENDPOINT_ALPHA_ID,
        "proxmox_vm_id": SHARED_VMID,
    },
}
VM_IN_BETA = {
    "id": 2002,
    "name": "shared-vm-beta",
    "cluster": {"id": CLUSTER_BETA_ID, "name": "beta"},
    "custom_fields": {
        "proxmox_endpoint_id": ENDPOINT_BETA_ID,
        "proxmox_vm_id": SHARED_VMID,
    },
}

_CLUSTER_NAME_TO_ID = {"alpha": CLUSTER_ALPHA_ID, "beta": CLUSTER_BETA_ID}


def _fake_vm_list_by_cluster(_nb, _endpoint, query):
    """Mimic NetBox VM filters for endpoint, cluster, and VMID."""
    vmid = query.get("cf_proxmox_vm_id")
    endpoint_id = query.get("cf_proxmox_endpoint_id")
    cluster_id = query.get("cluster_id")
    matches = [
        vm
        for vm in (VM_IN_ALPHA, VM_IN_BETA)
        if vm["custom_fields"]["proxmox_vm_id"] == vmid
        and (endpoint_id is None or vm["custom_fields"]["proxmox_endpoint_id"] == endpoint_id)
        and (cluster_id is None or vm["cluster"]["id"] == cluster_id)
    ]
    return matches


def test_build_vm_index_keys_by_endpoint_and_vmid():
    """Duplicate vmids across endpoints must both survive, keyed by endpoint and vmid."""
    index = _build_vm_index_by_proxmox_id([VM_IN_ALPHA, VM_IN_BETA])

    assert index[(ENDPOINT_ALPHA_ID, SHARED_VMID)] is VM_IN_ALPHA
    assert index[(ENDPOINT_BETA_ID, SHARED_VMID)] is VM_IN_BETA

    assert SHARED_VMID not in index
    assert len(index) == 2


@pytest.mark.asyncio
async def test_resolve_netbox_cluster_id_by_name_matches_and_caches(monkeypatch):
    calls: list[dict[str, object]] = []

    async def _fake_rest_list_async(_nb, _endpoint, query):
        calls.append(query)
        name = query.get("name")
        if name == "alpha":
            return [{"id": CLUSTER_ALPHA_ID, "name": "alpha"}]
        return []

    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_list_async",
        _fake_rest_list_async,
    )

    cache: dict[str, int | None] = {}
    assert (
        await resolve_netbox_cluster_id_by_name(object(), "alpha", cache=cache) == CLUSTER_ALPHA_ID
    )
    # Cached: a second call must not hit the REST layer again.
    assert (
        await resolve_netbox_cluster_id_by_name(object(), "alpha", cache=cache) == CLUSTER_ALPHA_ID
    )
    assert len(calls) == 1

    # Unknown cluster resolves to None instead of guessing a wrong cluster.
    assert await resolve_netbox_cluster_id_by_name(object(), "ghost", cache=cache) is None


@pytest.mark.asyncio
async def test_ensure_vm_record_resolves_correct_cluster(monkeypatch):
    captured_queries: list[dict[str, object]] = []

    async def _fake_rest_list_async(_nb, _endpoint, query):
        captured_queries.append(dict(query))
        return _fake_vm_list_by_cluster(_nb, _endpoint, query)

    async def _fake_resolve_cluster_id(_nb, cluster_name, **_kwargs):
        return _CLUSTER_NAME_TO_ID.get(str(cluster_name).strip())

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.helpers.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.helpers.resolve_netbox_cluster_id_by_name",
        _fake_resolve_cluster_id,
    )

    tag = SimpleNamespace(id=7, name="Proxbox", slug="proxbox")

    # Syncing within cluster "alpha" must resolve alpha's VM, never beta's.
    record_alpha, error_alpha = await ensure_vm_record(
        object(),
        SimpleNamespace(name="alpha", db_endpoint_id=ENDPOINT_ALPHA_ID),
        tag,
        vmid=SHARED_VMID,
        node="pve-a",
        vm_type="qemu",
        auto_create_vm=False,
    )
    assert error_alpha is None
    assert record_alpha is VM_IN_ALPHA

    # Syncing within cluster "beta" must resolve beta's VM.
    record_beta, error_beta = await ensure_vm_record(
        object(),
        SimpleNamespace(name="beta", db_endpoint_id=ENDPOINT_BETA_ID),
        tag,
        vmid=SHARED_VMID,
        node="pve-b",
        vm_type="qemu",
        auto_create_vm=False,
    )
    assert error_beta is None
    assert record_beta is VM_IN_BETA

    # Every NetBox VM lookup must have been endpoint-scoped.
    assert captured_queries, "expected at least one NetBox VM lookup"
    assert all("cf_proxmox_endpoint_id" in q for q in captured_queries)
    assert {q["cf_proxmox_endpoint_id"] for q in captured_queries} == {
        ENDPOINT_ALPHA_ID,
        ENDPOINT_BETA_ID,
    }


@pytest.mark.asyncio
async def test_resolve_netbox_virtual_machine_by_proxmox_id_scopes_by_endpoint(monkeypatch):
    captured_queries: list[dict[str, object]] = []

    async def _fake_rest_list_async(_nb, _endpoint, query):
        captured_queries.append(dict(query))
        return _fake_vm_list_by_cluster(_nb, _endpoint, query)

    async def _fake_resolve_cluster_id(_nb, cluster_name, **_kwargs):
        return _CLUSTER_NAME_TO_ID.get(str(cluster_name).strip())

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm."
        "resolve_netbox_cluster_id_by_name",
        _fake_resolve_cluster_id,
    )

    resolved_alpha = await _resolve_netbox_virtual_machine_by_proxmox_id(
        object(), SHARED_VMID, endpoint_id=ENDPOINT_ALPHA_ID, cluster_name="alpha"
    )
    resolved_beta = await _resolve_netbox_virtual_machine_by_proxmox_id(
        object(), SHARED_VMID, endpoint_id=ENDPOINT_BETA_ID, cluster_name="beta"
    )

    assert resolved_alpha == VM_IN_ALPHA
    assert resolved_beta == VM_IN_BETA
    assert {q.get("cf_proxmox_endpoint_id") for q in captured_queries} == {
        ENDPOINT_ALPHA_ID,
        ENDPOINT_BETA_ID,
    }
