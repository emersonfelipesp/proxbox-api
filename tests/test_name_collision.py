"""Unit tests for the VM name collision resolver."""

from __future__ import annotations

import pytest

from proxbox_api.services.name_collision import (
    NameResolution,
    _is_algorithmic_variant,
    _pick_suffix,
    resolve_unique_vm_name,
)


class TestPickSuffix:
    def test_no_collision_keeps_bare_name(self):
        assert _pick_suffix("gateway", set()) == ("gateway", 1)

    def test_first_collision_appends_two(self):
        assert _pick_suffix("gateway", {"gateway"}) == ("gateway (2)", 2)

    def test_second_collision_appends_three(self):
        assert _pick_suffix("gateway", {"gateway", "gateway (2)"}) == ("gateway (3)", 3)

    def test_finds_first_free_index(self):
        assert _pick_suffix(
            "gateway",
            {"gateway", "gateway (2)", "gateway (4)"},
        ) == ("gateway (3)", 3)

    def test_case_insensitive_match_preserves_candidate_casing(self):
        resolved, idx = _pick_suffix("Gateway", {"gateway"})
        assert resolved == "Gateway (2)"
        assert idx == 2

    def test_unicode_candidate(self):
        assert _pick_suffix("máquina", {"MÁQUINA"}) == ("máquina (2)", 2)

    def test_long_collision_chain_terminates(self):
        used = {"vm" if i == 1 else f"vm ({i})" for i in range(1, 6)}
        used.add("vm")
        assert _pick_suffix("vm", used) == ("vm (6)", 6)


class TestIsAlgorithmicVariant:
    def test_bare_match_is_algorithmic(self):
        assert _is_algorithmic_variant("gateway", "gateway") is True

    def test_suffixed_match_is_algorithmic(self):
        assert _is_algorithmic_variant("gateway (2)", "gateway") is True
        assert _is_algorithmic_variant("gateway (10)", "gateway") is True

    def test_case_insensitive(self):
        assert _is_algorithmic_variant("Gateway (2)", "gateway") is True

    def test_unrelated_name_not_algorithmic(self):
        assert _is_algorithmic_variant("gateway-prod", "gateway") is False
        assert _is_algorithmic_variant("gw", "gateway") is False

    def test_non_integer_suffix_not_algorithmic(self):
        assert _is_algorithmic_variant("gateway (prod)", "gateway") is False


@pytest.mark.asyncio
class TestResolveUniqueVmName:
    async def test_no_collision_returns_bare_name(self):
        used: set[str] = set()
        resolution = await resolve_unique_vm_name(
            None,
            netbox_cluster_id=1,
            proxmox_cluster_name="cluster-a",
            candidate="gateway",
            proxmox_vmid=100,
            used_names_in_cluster=used,
            existing_vm_by_vmid={},
        )
        assert isinstance(resolution, NameResolution)
        assert resolution.resolved_name == "gateway"
        assert resolution.suffix_index == 1
        assert resolution.is_collision is False
        assert resolution.operator_renamed is False
        assert "gateway" in used

    async def test_collision_appends_suffix(self):
        used: set[str] = {"gateway"}
        resolution = await resolve_unique_vm_name(
            None,
            netbox_cluster_id=1,
            proxmox_cluster_name="cluster-a",
            candidate="gateway",
            proxmox_vmid=101,
            used_names_in_cluster=used,
            existing_vm_by_vmid={},
        )
        assert resolution.resolved_name == "gateway (2)"
        assert resolution.suffix_index == 2
        assert resolution.is_collision is True
        assert resolution.operator_renamed is False
        assert "gateway (2)" in used

    async def test_operator_rename_is_preserved(self):
        used: set[str] = set()
        resolution = await resolve_unique_vm_name(
            None,
            netbox_cluster_id=1,
            proxmox_cluster_name="cluster-a",
            candidate="gateway",
            proxmox_vmid=100,
            used_names_in_cluster=used,
            existing_vm_by_vmid={100: {"name": "gateway-prod"}},
        )
        assert resolution.resolved_name == "gateway-prod"
        assert resolution.operator_renamed is True
        assert resolution.is_collision is False
        assert resolution.suffix_index == 1
        assert "gateway-prod" in used

    async def test_algorithmic_existing_name_not_treated_as_operator_rename(self):
        used: set[str] = {"gateway"}
        resolution = await resolve_unique_vm_name(
            None,
            netbox_cluster_id=1,
            proxmox_cluster_name="cluster-a",
            candidate="gateway",
            proxmox_vmid=200,
            used_names_in_cluster=used,
            existing_vm_by_vmid={200: {"name": "gateway (2)"}},
        )
        assert resolution.operator_renamed is False
        assert resolution.resolved_name == "gateway (2)"
        assert resolution.suffix_index == 2
        assert resolution.is_collision is True

    async def test_idempotent_across_runs(self):
        # First sync.
        used1: set[str] = set()
        first = await resolve_unique_vm_name(
            None,
            netbox_cluster_id=1,
            proxmox_cluster_name="cluster-a",
            candidate="gateway",
            proxmox_vmid=100,
            used_names_in_cluster=used1,
            existing_vm_by_vmid={},
        )
        # Second sync — same input, caller still seeds used set from snapshot
        # but excludes the record this VMID owns.
        used2: set[str] = set()  # snapshot trimmed of self
        second = await resolve_unique_vm_name(
            None,
            netbox_cluster_id=1,
            proxmox_cluster_name="cluster-a",
            candidate="gateway",
            proxmox_vmid=100,
            used_names_in_cluster=used2,
            existing_vm_by_vmid={100: {"name": "gateway"}},
        )
        assert first.resolved_name == second.resolved_name == "gateway"
        assert second.operator_renamed is False

    async def test_no_cluster_skips_operator_rename(self):
        used: set[str] = set()
        resolution = await resolve_unique_vm_name(
            None,
            netbox_cluster_id=None,
            proxmox_cluster_name="cluster-a",
            candidate="gateway",
            proxmox_vmid=100,
            used_names_in_cluster=used,
            existing_vm_by_vmid={100: {"name": "gateway-prod"}},
        )
        assert resolution.operator_renamed is False
        assert resolution.resolved_name == "gateway"
