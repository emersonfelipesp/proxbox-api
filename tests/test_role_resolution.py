"""Tests for issue #364 hierarchical default role resolution and lock.

Two surfaces are covered here:

* ``compute_role_snapshot_decision`` — the nine-case pure decision tree.
* ``resolve_default_role_id`` — the four-tier resolver that walks node →
  endpoint → plugin singleton.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.services.sync.role_resolution import (
    RoleSnapshotDecision,
    apply_role_snapshot_policy,
    compensate_failed_role_snapshot,
    compute_role_snapshot_decision,
    resolve_default_role_id,
    resolve_snapshot_read_from_scan,
)
from proxbox_api.services.sync.sync_state_reader import VMRoleSnapshotScan

# --------------------------------------------------------------------------- #
# Pure decision function: the nine-case matrix.
# --------------------------------------------------------------------------- #


def test_case_1_fresh_create_plugin_global_only() -> None:
    """Snapshot None + role None (fresh create) → write both = plugin default."""
    decision = compute_role_snapshot_decision(
        existing_role_id=None,
        existing_snapshot_id=None,
        desired_role_id=11,  # plugin global resolves here
        overwrite_vm_role=False,
    )
    assert decision == RoleSnapshotDecision(
        role_value=11,
        snapshot_value=11,
        write_role=True,
        write_snapshot=True,
    )


def test_case_2_fresh_create_endpoint_override() -> None:
    """Resolver-supplied desired (endpoint tier) is written verbatim on fresh create."""
    decision = compute_role_snapshot_decision(
        existing_role_id=None,
        existing_snapshot_id=None,
        desired_role_id=22,  # endpoint tier resolves here
        overwrite_vm_role=False,
    )
    assert decision.role_value == 22
    assert decision.snapshot_value == 22
    assert decision.write_role
    assert decision.write_snapshot


def test_case_3_fresh_create_node_override() -> None:
    """Node tier wins → fresh-create writes node id to both fields."""
    decision = compute_role_snapshot_decision(
        existing_role_id=None,
        existing_snapshot_id=None,
        desired_role_id=33,  # node tier resolves here
        overwrite_vm_role=False,
    )
    assert decision.role_value == 33
    assert decision.snapshot_value == 33
    assert decision.write_role
    assert decision.write_snapshot


def test_case_4_update_steady_state_omits_both() -> None:
    """existing == snapshot == desired → no PATCH writes for role or snapshot."""
    decision = compute_role_snapshot_decision(
        existing_role_id=7,
        existing_snapshot_id=7,
        desired_role_id=7,
        overwrite_vm_role=False,
    )
    assert not decision.write_role
    assert not decision.write_snapshot


def test_case_5_update_roll_forward_writes_both() -> None:
    """existing == snapshot != desired → write desired to role and snapshot."""
    decision = compute_role_snapshot_decision(
        existing_role_id=7,
        existing_snapshot_id=7,
        desired_role_id=9,
        overwrite_vm_role=False,
    )
    assert decision.write_role
    assert decision.write_snapshot
    assert decision.role_value == 9
    assert decision.snapshot_value == 9


def test_case_6_operator_lock_skips_both() -> None:
    """existing != snapshot, overwrite=False → preserve operator edit forever."""
    decision = compute_role_snapshot_decision(
        existing_role_id=42,  # operator changed role to 42
        existing_snapshot_id=7,  # snapshot still records last sync (7)
        desired_role_id=9,
        overwrite_vm_role=False,
    )
    assert not decision.write_role
    assert not decision.write_snapshot


def test_case_7_operator_lock_released_writes_both() -> None:
    """existing != snapshot, overwrite=True → restamp role and snapshot."""
    decision = compute_role_snapshot_decision(
        existing_role_id=42,
        existing_snapshot_id=7,
        desired_role_id=9,
        overwrite_vm_role=True,
    )
    assert decision.write_role
    assert decision.write_snapshot
    assert decision.role_value == 9
    assert decision.snapshot_value == 9


def test_case_8_upgrade_backfill_with_role_writes_snapshot_only() -> None:
    """snapshot None, role non-NULL → capture operator intent: snapshot only."""
    decision = compute_role_snapshot_decision(
        existing_role_id=15,
        existing_snapshot_id=None,
        desired_role_id=9,
        overwrite_vm_role=False,
    )
    assert not decision.write_role  # role untouched
    assert decision.write_snapshot
    assert decision.snapshot_value == 15  # snapshot captures current role


def test_case_9_upgrade_backfill_role_null_writes_both() -> None:
    """snapshot None, role None → treat as fresh-create-style apply."""
    decision = compute_role_snapshot_decision(
        existing_role_id=None,
        existing_snapshot_id=None,
        desired_role_id=9,
        overwrite_vm_role=False,
    )
    assert decision.write_role
    assert decision.write_snapshot
    assert decision.role_value == 9
    assert decision.snapshot_value == 9


# --------------------------------------------------------------------------- #
# Edge cases not in the 9-case matrix but worth pinning.
# --------------------------------------------------------------------------- #


def test_no_desired_no_existing_skips_writes() -> None:
    """Nothing to write: no tier yielded a role and the VM has none."""
    decision = compute_role_snapshot_decision(
        existing_role_id=None,
        existing_snapshot_id=None,
        desired_role_id=None,
        overwrite_vm_role=False,
    )
    assert not decision.write_role
    assert not decision.write_snapshot


def test_overwrite_true_no_desired_clears_snapshot_writes() -> None:
    """Operator override with desired=None must not blindly null the role."""
    decision = compute_role_snapshot_decision(
        existing_role_id=42,
        existing_snapshot_id=7,
        desired_role_id=None,
        overwrite_vm_role=True,
    )
    # write_role/snapshot gated on desired_role_id being non-None.
    assert not decision.write_role
    assert not decision.write_snapshot


def test_apply_policy_hard_fences_operator_role_from_reconcile_patch() -> None:
    """An operator-owned role is removed from both payload writes and allowlist."""
    payload, patchable_fields, decision = apply_role_snapshot_policy(
        existing_record={"id": 61, "role": {"id": 42}},
        existing_snapshot_id=11,
        desired_payload={"name": "vm-61", "role": 20, "memory": 4096},
        patchable_fields={"name", "role", "memory"},
        overwrite_vm_role=False,
    )

    assert payload["role"] == 20
    assert "role" not in patchable_fields
    assert patchable_fields == frozenset({"name", "memory"})
    assert not decision.write_role
    assert not decision.write_snapshot


def test_apply_policy_backfills_snapshot_without_role_patch() -> None:
    """A pre-snapshot VM captures its current role without allowing a role PATCH."""
    _payload, patchable_fields, decision = apply_role_snapshot_policy(
        existing_record={"id": 62, "role": 42},
        existing_snapshot_id=None,
        desired_payload={"name": "vm-62", "role": 20},
        patchable_fields={"name", "role"},
        overwrite_vm_role=False,
    )

    assert "role" not in patchable_fields
    assert decision.snapshot_value == 42
    assert decision.write_snapshot


def test_apply_policy_does_not_claim_ownership_after_unverified_read() -> None:
    """Unavailable or ambiguous sidecar evidence preserves role without backfill."""
    _payload, patchable_fields, decision = apply_role_snapshot_policy(
        existing_record={"id": 63, "role": 42},
        existing_snapshot_id=None,
        desired_payload={"name": "vm-63", "role": 20},
        patchable_fields={"name", "role"},
        overwrite_vm_role=False,
        snapshot_read_verified=False,
    )

    assert "role" not in patchable_fields
    assert not decision.write_role
    assert not decision.write_snapshot


def test_failed_batch_scan_does_not_claim_snapshot_ownership() -> None:
    read = resolve_snapshot_read_from_scan(
        VMRoleSnapshotScan(values={}, read_verified=False),
        {"id": 64},
    )

    assert read.snapshot_id is None
    assert not read.verified


@pytest.mark.asyncio
async def test_role_compensation_retries_until_restored_role_is_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proxbox_api.services.sync import role_resolution

    patch_attempts = 0
    read_attempts = 0

    async def _patch(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal patch_attempts
        patch_attempts += 1
        return {"id": 65}

    async def _read(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal read_attempts
        read_attempts += 1
        return {"id": 65, "role": {"id": 11 if read_attempts == 2 else 20}}

    monkeypatch.setattr(role_resolution, "rest_patch_async", _patch)
    monkeypatch.setattr(role_resolution, "rest_first_async", _read)

    await compensate_failed_role_snapshot(
        object(),
        virtual_machine_id=65,
        previous_role_id=11,
    )

    assert patch_attempts == 2
    assert read_attempts == 2


# --------------------------------------------------------------------------- #
# Resolver tests: walk node → endpoint → plugin singleton.
# --------------------------------------------------------------------------- #


@pytest.fixture
def patch_resolver(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Provide a helper that monkeypatches the two collaborators the resolver uses."""

    state: dict[str, Any] = {"first_calls": []}

    def install(
        *,
        first_results: dict[str, dict[str, Any] | None],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        async def _fake_first(
            _nb: object, path: str, *, query: dict[str, object] | None = None
        ) -> Any:
            state["first_calls"].append((path, dict(query or {})))
            for needle, value in first_results.items():
                if needle in path:
                    return value
            return None

        def _fake_settings(_nb: object = None) -> dict[str, Any]:
            return settings

        monkeypatch.setattr(
            "proxbox_api.services.sync.role_resolution.rest_first_async", _fake_first
        )
        monkeypatch.setattr(
            "proxbox_api.services.sync.role_resolution.get_settings", _fake_settings
        )
        return state

    return install


@pytest.mark.asyncio
async def test_resolver_returns_node_default(patch_resolver: Any) -> None:
    """When the node row has default_role_qemu set, that id wins."""
    patch_resolver(
        first_results={
            "proxmox-nodes": {"default_role_qemu": 101, "endpoint": 5},
        },
        settings={"default_role_qemu_id": 999, "default_role_lxc_id": None},
    )
    result = await resolve_default_role_id(
        object(), vm_type="qemu", node_name="pve01", cluster_id=1
    )
    assert result == 101


@pytest.mark.asyncio
async def test_resolver_falls_through_to_endpoint(patch_resolver: Any) -> None:
    """Node row has no default → endpoint default applies."""
    state = patch_resolver(
        first_results={
            "proxmox-nodes": {"default_role_qemu": None, "endpoint": 5},
            "endpoints/proxmox": {"default_role_qemu": 202},
        },
        settings={"default_role_qemu_id": 999, "default_role_lxc_id": None},
    )
    result = await resolve_default_role_id(
        object(), vm_type="qemu", node_name="pve01", cluster_id=1
    )
    assert result == 202
    # endpoint lookup should have been queried by id=5 (the node's endpoint FK).
    endpoint_calls = [c for c in state["first_calls"] if "endpoints/proxmox" in c[0]]
    assert endpoint_calls
    assert endpoint_calls[0][1].get("id") == 5


@pytest.mark.asyncio
async def test_resolver_falls_through_to_plugin_singleton(patch_resolver: Any) -> None:
    """Node and endpoint both null → plugin singleton applies."""
    patch_resolver(
        first_results={
            "proxmox-nodes": {"default_role_qemu": None, "endpoint": 5},
            "endpoints/proxmox": {"default_role_qemu": None},
        },
        settings={"default_role_qemu_id": 303, "default_role_lxc_id": None},
    )
    result = await resolve_default_role_id(
        object(), vm_type="qemu", node_name="pve01", cluster_id=1
    )
    assert result == 303


@pytest.mark.asyncio
async def test_resolver_returns_none_when_no_tier_set(patch_resolver: Any) -> None:
    """All tiers empty → return None (caller leaves role unset)."""
    patch_resolver(
        first_results={
            "proxmox-nodes": {"default_role_qemu": None, "endpoint": None},
        },
        settings={"default_role_qemu_id": None, "default_role_lxc_id": None},
    )
    result = await resolve_default_role_id(
        object(), vm_type="qemu", node_name="pve01", cluster_id=1
    )
    assert result is None


@pytest.mark.asyncio
async def test_resolver_picks_lxc_field(patch_resolver: Any) -> None:
    """vm_type='lxc' reads the matching lxc tier field, not qemu."""
    patch_resolver(
        first_results={
            "proxmox-nodes": {"default_role_lxc": 404, "default_role_qemu": 99},
        },
        settings={"default_role_qemu_id": None, "default_role_lxc_id": 888},
    )
    result = await resolve_default_role_id(object(), vm_type="lxc", node_name="pve01", cluster_id=1)
    assert result == 404


@pytest.mark.asyncio
async def test_resolver_rejects_unknown_vm_type(patch_resolver: Any) -> None:
    patch_resolver(
        first_results={},
        settings={"default_role_qemu_id": 1, "default_role_lxc_id": 2},
    )
    result = await resolve_default_role_id(
        object(), vm_type="bogus", node_name="pve01", cluster_id=1
    )
    assert result is None


@pytest.mark.asyncio
async def test_resolver_skips_node_lookup_without_node_name(patch_resolver: Any) -> None:
    """Without a node name, jump straight to the plugin singleton."""
    state = patch_resolver(
        first_results={},
        settings={"default_role_qemu_id": 505, "default_role_lxc_id": None},
    )
    result = await resolve_default_role_id(
        object(), vm_type="qemu", node_name=None, cluster_id=None
    )
    assert result == 505
    assert state["first_calls"] == []  # never queried any NetBox endpoint
