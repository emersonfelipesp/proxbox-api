"""Regression tests for cross-endpoint Proxmox VMID collisions.

When the same Proxmox VM ID (vmid) exists on two standalone endpoints, NetBox
VM resolution must be scoped by ``(proxmox_endpoint_id, vmid)``. Keying on vmid
alone, or on a fragile cluster relation, mapped interfaces/IPs to the wrong
VirtualMachine and dropped duplicate vmids.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from proxbox_api.routes.virtualization.virtual_machines import sync_vm
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    _build_vm_candidates_by_proxmox_id,
    _build_vm_index_by_proxmox_id,
    _resolve_netbox_virtual_machine_by_proxmox_id,
    _resolve_vm_from_index_or_unique_vmid,
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
    "proxmox_endpoint_id": ENDPOINT_ALPHA_ID,
    "proxmox_vm_id": SHARED_VMID,
}
VM_IN_BETA = {
    "id": 2002,
    "name": "shared-vm-beta",
    "cluster": {"id": CLUSTER_BETA_ID, "name": "beta"},
    "proxmox_endpoint_id": ENDPOINT_BETA_ID,
    "proxmox_vm_id": SHARED_VMID,
}

_CLUSTER_NAME_TO_ID = {"alpha": CLUSTER_ALPHA_ID, "beta": CLUSTER_BETA_ID}


def _duplicate_vmid_caller_inputs() -> dict[str, object]:
    return {
        "netbox_session": object(),
        "pxs": [
            SimpleNamespace(name="alpha", cluster_name="alpha", db_endpoint_id=ENDPOINT_ALPHA_ID),
            SimpleNamespace(name="beta", cluster_name="beta", db_endpoint_id=ENDPOINT_BETA_ID),
        ],
        "cluster_status": [SimpleNamespace(name="alpha"), SimpleNamespace(name="beta")],
        "cluster_resources": [
            {
                "alpha": [
                    {
                        "type": "qemu",
                        "name": "shared-vm-alpha",
                        "node": "pve-a",
                        "vmid": SHARED_VMID,
                    }
                ]
            },
            {
                "beta": [
                    {
                        "type": "qemu",
                        "name": "shared-vm-beta",
                        "node": "pve-b",
                        "vmid": SHARED_VMID,
                    }
                ]
            },
        ],
        "tag": SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
    }


def _install_duplicate_vmid_caller_stubs(monkeypatch, config_factory) -> None:
    async def _fake_load_snapshot(_nb):
        return [dict(VM_IN_ALPHA), dict(VM_IN_BETA)]

    async def _fake_hydrate(_nb, vms, *, require_all):
        assert require_all is False
        return vms

    async def _fake_resolve_cluster_id(_nb, cluster_name, **_kwargs):
        return _CLUSTER_NAME_TO_ID[str(cluster_name)]

    def _fake_get_vm_config(**kwargs):
        endpoint_id = kwargs["pxs"][0].db_endpoint_id
        return config_factory(endpoint_id)

    async def _fake_guest_sidecars(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sync_vm, "_load_netbox_virtual_machine_snapshot", _fake_load_snapshot)
    monkeypatch.setattr(sync_vm, "hydrate_vm_identities_from_sidecars", _fake_hydrate)
    monkeypatch.setattr(sync_vm, "resolve_netbox_cluster_id_by_name", _fake_resolve_cluster_id)
    monkeypatch.setattr(sync_vm, "resolve_vm_sync_concurrency", lambda: 1)
    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(sync_vm, "reconcile_guest_vm_interfaces", _fake_guest_sidecars)


def _vm_for_endpoint(endpoint_id: object) -> dict[str, object] | None:
    if endpoint_id == ENDPOINT_ALPHA_ID:
        return VM_IN_ALPHA
    if endpoint_id == ENDPOINT_BETA_ID:
        return VM_IN_BETA
    return None


def test_build_vm_index_keys_by_endpoint_and_vmid():
    """Duplicate vmids across endpoints must both survive, keyed by endpoint and vmid."""
    index = _build_vm_index_by_proxmox_id([VM_IN_ALPHA, VM_IN_BETA])

    assert index[(ENDPOINT_ALPHA_ID, SHARED_VMID)] is VM_IN_ALPHA
    assert index[(ENDPOINT_BETA_ID, SHARED_VMID)] is VM_IN_BETA

    assert SHARED_VMID not in index
    assert len(index) == 2


def test_interface_ip_resolution_selects_each_endpoint_owned_vm() -> None:
    snapshot = [VM_IN_ALPHA, VM_IN_BETA]
    index = _build_vm_index_by_proxmox_id(snapshot)
    candidates = _build_vm_candidates_by_proxmox_id(snapshot)

    alpha = _resolve_vm_from_index_or_unique_vmid(
        index,
        candidates,
        endpoint_id=ENDPOINT_ALPHA_ID,
        raw_vmid=SHARED_VMID,
        cluster_name="alpha",
        sync_context="IP address",
    )
    beta = _resolve_vm_from_index_or_unique_vmid(
        index,
        candidates,
        endpoint_id=ENDPOINT_BETA_ID,
        raw_vmid=SHARED_VMID,
        cluster_name="beta",
        sync_context="IP address",
    )

    assert alpha is VM_IN_ALPHA
    assert beta is VM_IN_BETA


@pytest.mark.asyncio
async def test_create_only_vm_interfaces_targets_each_endpoint_owned_duplicate_vmid(
    monkeypatch,
) -> None:
    captured_payloads: list[dict[str, object]] = []
    _install_duplicate_vmid_caller_stubs(
        monkeypatch,
        lambda _endpoint_id: {"net0": "virtio=AA:BB:CC:DD:EE:FF"},
    )

    async def _fake_bulk_reconcile(_nb, payloads, **_kwargs):
        captured_payloads.extend(dict(payload) for payload in payloads)
        created = [
            {"id": 3001, **payloads[0]},
            {"id": 3002, **payloads[1]},
        ]
        return created, {("net0", 1001): 3001, ("net0", 2002): 3002}

    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interfaces",
        _fake_bulk_reconcile,
    )

    await sync_vm.create_only_vm_interfaces(
        **_duplicate_vmid_caller_inputs(),
        sync_mac=False,
    )

    assert {(payload["name"], payload["virtual_machine"]) for payload in captured_payloads} == {
        ("net0", 1001),
        ("net0", 2002),
    }


@pytest.mark.asyncio
async def test_create_only_vm_ip_addresses_targets_each_endpoint_owned_duplicate_vmid(
    monkeypatch,
) -> None:
    captured_payloads: list[dict[str, object]] = []
    interface_ids = {1001: 3001, 2002: 3002}

    def _config_for_endpoint(endpoint_id: int) -> dict[str, object]:
        address = "192.0.2.10/24" if endpoint_id == ENDPOINT_ALPHA_ID else "198.51.100.20/24"
        return {"net0": f"virtio=AA:BB:CC:DD:EE:FF,ip={address}"}

    _install_duplicate_vmid_caller_stubs(monkeypatch, _config_for_endpoint)

    async def _fake_rest_list(_nb, path, *, query=None):
        assert path == "/api/virtualization/interfaces/"
        vm_id = int(query["virtual_machine_id"])
        return [{"id": interface_ids[vm_id], "name": "net0", "virtual_machine": vm_id}]

    async def _fake_rest_first(*_args, **_kwargs):
        return None

    async def _fake_bulk_reconcile(_nb, payloads, **_kwargs):
        captured_payloads.extend(dict(payload) for payload in payloads)
        return [{"id": index, **payload} for index, payload in enumerate(payloads, start=4001)]

    async def _fake_cleanup(*_args, **_kwargs):
        return 0

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _fake_rest_list)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_first_async", _fake_rest_first)
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interface_ips",
        _fake_bulk_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.cleanup_stale_ips_for_interface",
        _fake_cleanup,
    )

    await sync_vm.create_only_vm_ip_addresses(**_duplicate_vmid_caller_inputs())

    assert {
        (payload["address"], payload["assigned_object_id"]) for payload in captured_payloads
    } == {
        ("192.0.2.10/24", 3001),
        ("198.51.100.20/24", 3002),
    }


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
        vm = _vm_for_endpoint(query.get("proxmox_endpoint_raw_id"))
        return (
            [
                {
                    "virtual_machine": {"id": vm["id"]},
                    "proxmox_vm_id": SHARED_VMID,
                    "proxmox_endpoint_raw_id": query.get("proxmox_endpoint_raw_id"),
                }
            ]
            if vm
            else []
        )

    async def _fake_rest_first_async(_nb, _endpoint, *, query):
        return next((vm for vm in (VM_IN_ALPHA, VM_IN_BETA) if vm["id"] == query["id"]), None)

    async def _fake_resolve_cluster_id(_nb, cluster_name, **_kwargs):
        return _CLUSTER_NAME_TO_ID.get(str(cluster_name).strip())

    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_first_async",
        _fake_rest_first_async,
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
    sidecar_queries = [query for query in captured_queries if "proxmox_endpoint_raw_id" in query]
    assert sidecar_queries
    assert {q["proxmox_endpoint_raw_id"] for q in sidecar_queries} == {
        ENDPOINT_ALPHA_ID,
        ENDPOINT_BETA_ID,
    }


@pytest.mark.asyncio
async def test_resolve_netbox_virtual_machine_by_proxmox_id_scopes_by_endpoint(monkeypatch):
    captured_queries: list[dict[str, object]] = []

    async def _fake_rest_list_async(_nb, _endpoint, query):
        captured_queries.append(dict(query))
        vm = _vm_for_endpoint(query.get("proxmox_endpoint_raw_id"))
        return (
            [
                {
                    "virtual_machine": {"id": vm["id"]},
                    "proxmox_vm_id": SHARED_VMID,
                    "proxmox_endpoint_raw_id": query.get("proxmox_endpoint_raw_id"),
                }
            ]
            if vm
            else []
        )

    async def _fake_rest_first_async(_nb, _endpoint, *, query):
        return next((vm for vm in (VM_IN_ALPHA, VM_IN_BETA) if vm["id"] == query["id"]), None)

    async def _fake_resolve_cluster_id(_nb, cluster_name, **_kwargs):
        return _CLUSTER_NAME_TO_ID.get(str(cluster_name).strip())

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_first_async",
        _fake_rest_first_async,
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
    sidecar_queries = [query for query in captured_queries if "proxmox_endpoint_raw_id" in query]
    assert {q["proxmox_endpoint_raw_id"] for q in sidecar_queries} == {
        ENDPOINT_ALPHA_ID,
        ENDPOINT_BETA_ID,
    }
