"""Regression tests: the NetBox ``platform`` field is populated from the guest OS.

NetBox virtual machines carry a ``platform`` field and Proxbox never populated it —
``platform`` appeared nowhere in `proxbox_api/services/` or
`proxbox_api/proxmox_to_netbox/`, and ``ostype`` existed in the internal schema without
ever being propagated. Two sources were already reachable during a sync:

* ``ostype`` from the VM configuration — coarse, always present, and free;
* the QEMU guest agent's ``get-osinfo`` — exact, but one extra Proxmox request per VM.

The mapping expectations below are **transcribed** from the Proxmox VE API documentation
for the QEMU and LXC ``ostype`` parameters, not read back out of the table under test. A
table derived from the mapping would be satisfied by any mapping, including a wrong one.
"""

from __future__ import annotations

import pytest

from proxbox_api.proxmox_to_netbox.guest_os import (
    platform_from_guest_agent,
    platform_from_ostype,
    platform_slug,
    resolve_platform_name,
)
from proxbox_api.schemas.sync import SyncBehaviorFlags, SyncOverwriteFlags
from proxbox_api.services.sync.vm_helpers import (
    _compute_vm_patchable_fields,
    normalize_current_virtual_machine_payload,
)

# Transcribed once, by hand, from the Proxmox VE ostype documentation.
TRANSCRIBED_OSTYPES: dict[str, str] = {
    "l24": "Linux (kernel 2.4)",
    "l26": "Linux (kernel 2.6 or newer)",
    "win10": "Windows 10",
    "win11": "Windows 11",
    "w2k19": None,  # not a Proxmox ostype value; must not resolve
    "wxp": "Windows XP",
    "solaris": "Solaris",
    "debian": "Debian",
    "ubuntu": "Ubuntu",
    "alpine": "Alpine Linux",
    "archlinux": "Arch Linux",
}


@pytest.mark.parametrize(("ostype", "expected"), sorted(TRANSCRIBED_OSTYPES.items()))
def test_ostype_maps_to_the_transcribed_platform(ostype: str, expected: str | None) -> None:
    assert platform_from_ostype(ostype) == expected


def test_ostype_matching_is_case_and_whitespace_insensitive() -> None:
    assert platform_from_ostype("  L26 ") == "Linux (kernel 2.6 or newer)"


@pytest.mark.parametrize("ostype", [None, "", "   ", "not-a-real-ostype", 42, [], {}, object()])
def test_unknown_or_hostile_ostype_leaves_the_platform_unset(ostype: object) -> None:
    """Guessing would put a wrong operating system on an inventory page."""
    assert platform_from_ostype(ostype) is None


# --------------------------------------------------------------------------------------
# Guest-agent refinement
# --------------------------------------------------------------------------------------

# Transcribed verbatim from the reporter's example payload.
REPORTED_OSINFO = {"name": "Ubuntu", "version-id": "22.04", "pretty-name": "Ubuntu 22.04.5 LTS"}


def test_guest_agent_uses_name_and_version_not_pretty_name() -> None:
    """``pretty-name`` embeds the patch level, so it would mint a platform per update."""
    assert platform_from_guest_agent(REPORTED_OSINFO) == "Ubuntu 22.04"


def test_a_patch_level_bump_does_not_produce_a_new_platform() -> None:
    """The property the naming rule exists for."""
    before = platform_from_guest_agent(
        {"name": "Ubuntu", "version-id": "22.04", "pretty-name": "Ubuntu 22.04.5 LTS"}
    )
    after = platform_from_guest_agent(
        {"name": "Ubuntu", "version-id": "22.04", "pretty-name": "Ubuntu 22.04.6 LTS"}
    )
    assert before == after
    assert platform_slug(before) == platform_slug(after)


def test_proxmox_wraps_the_agent_payload_under_result() -> None:
    assert platform_from_guest_agent({"result": REPORTED_OSINFO}) == "Ubuntu 22.04"


def test_missing_version_falls_back_to_the_bare_name() -> None:
    assert platform_from_guest_agent({"name": "FreeBSD"}) == "FreeBSD"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not-a-dict",
        [],
        42,
        {},
        {"name": None},
        {"name": 42},
        {"name": ""},
        {"name": "   "},
        {"name": "Ubuntu", "version-id": 2204},
        {"name": "Ubuntu", "version-id": ["22.04"]},
        {"result": "not-a-dict"},
        {"result": {"name": None}},
        {"pretty-name": "Ubuntu 22.04.5 LTS"},
    ],
)
def test_hostile_guest_agent_payloads_degrade_without_raising(payload: object) -> None:
    """This reads data produced by a guest the operator may not control.

    A reader of external data that throws is worse than one that returns nothing: the
    platform is an inventory nicety, and losing a VM's sync over it would be a bad trade.
    """
    result = platform_from_guest_agent(payload)
    assert result is None or isinstance(result, str)


def test_oversized_agent_values_are_bounded() -> None:
    """NetBox's Platform.name is a 100-character field."""
    name = platform_from_guest_agent({"name": "A" * 500, "version-id": "1"})
    assert name is not None
    assert len(name) <= 100


def test_embedded_whitespace_is_collapsed() -> None:
    assert platform_from_guest_agent({"name": "Red   Hat\tEnterprise", "version-id": "9"}) == (
        "Red Hat Enterprise 9"
    )


# --------------------------------------------------------------------------------------
# Precedence and slugs
# --------------------------------------------------------------------------------------


def test_agent_value_is_preferred_over_ostype() -> None:
    assert resolve_platform_name(ostype="l26", guest_agent_osinfo=REPORTED_OSINFO) == "Ubuntu 22.04"


def test_ostype_is_the_floor_when_the_agent_says_nothing() -> None:
    for osinfo in (None, {}, "garbage", {"name": None}):
        assert resolve_platform_name(ostype="l26", guest_agent_osinfo=osinfo) == (
            "Linux (kernel 2.6 or newer)"
        )


def test_both_absent_leaves_the_platform_unset() -> None:
    assert resolve_platform_name(ostype=None, guest_agent_osinfo=None) is None


def test_slug_is_stable_across_calls() -> None:
    """Slug matching is what makes repeated syncs converge on one record."""
    name = "Linux (kernel 2.6 or newer)"
    assert platform_slug(name) == platform_slug(name) == "linux-kernel-2-6-or-newer"


def test_slug_is_netbox_safe_and_bounded() -> None:
    slug = platform_slug("Ünïcödé  OS!! " + "x" * 200)
    assert slug
    assert len(slug) <= 100
    assert all(char.isalnum() or char == "-" for char in slug)
    assert not slug.startswith("-") and not slug.endswith("-")


def test_distinct_platforms_do_not_collide_on_slug() -> None:
    names = set(TRANSCRIBED_OSTYPES.values()) - {None}
    slugs = {platform_slug(name) for name in names}
    assert len(slugs) == len(names)


# --------------------------------------------------------------------------------------
# Reconciler gating
# --------------------------------------------------------------------------------------


def test_platform_is_never_patched_on_an_existing_vm() -> None:
    """Set at creation, never afterwards.

    Proxbox has never owned this field, so an operator may well have set it by hand and
    taking it over on the first sync after upgrading would be a regression dressed as a
    feature. Making that operator-tunable would mean adding an `overwrite_*` flag, and
    that set is a CI-enforced cross-repo contract that must change in both repos in the
    same release -- see `test_overwrite_flags_contract.py`.
    """
    for flags in (SyncOverwriteFlags(), None):
        assert "platform" not in _compute_vm_patchable_fields(flags)


def test_no_overwrite_flag_was_invented_for_platform() -> None:
    """Guard the contract, not just today's behaviour.

    Adding `overwrite_vm_platform` to the schema without updating both repos' manifests
    is exactly the drift `contracts/overwrite_flags.json` exists to catch. This fails
    fast and locally if someone reaches for that shortcut again.
    """
    assert "overwrite_vm_platform" not in SyncOverwriteFlags.model_fields


def test_reconciler_diff_can_see_the_current_platform() -> None:
    """Without this the field could never be patched even with the gate on."""
    normalized = normalize_current_virtual_machine_payload({"name": "vm1", "platform": 7})
    assert normalized["platform"] == 7


def test_guest_agent_refinement_is_opt_in() -> None:
    """It costs one extra Proxmox request per eligible VM, so it must not be imposed."""
    assert SyncBehaviorFlags().sync_vm_platform_from_guest_agent is False


# --------------------------------------------------------------------------------------
# Request amplification
#
# An estate's virtual machines share a handful of operating systems. Reconciling a
# platform per VM would mean 500 NetBox lookups for perhaps five platforms, which is the
# same request-amplification shape this project fixes elsewhere.
# --------------------------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _counting_upsert(counter: list[str]):
    from types import SimpleNamespace

    class _Record:
        def serialize(self):
            return {"id": 7}

    async def _upsert(nb, *, name, slug, tag_refs=None):
        counter.append(slug)
        return SimpleNamespace(record=_Record())

    return _upsert


def test_repeated_guests_reconcile_the_platform_once(monkeypatch) -> None:
    from proxbox_api.services import netbox_writers
    from proxbox_api.services.sync.vm_create import ensure_vm_platform

    calls: list[str] = []
    monkeypatch.setattr(netbox_writers, "upsert_platform", _counting_upsert(calls))

    cache: dict[str, int | None] = {}
    for _ in range(50):
        assert _run(ensure_vm_platform(object(), ostype="l26", cache=cache)) == 7

    assert calls == ["linux-kernel-2-6-or-newer"], (
        f"expected exactly one reconcile for 50 identical guests, got {len(calls)}"
    )


def test_distinct_guests_each_reconcile_once(monkeypatch) -> None:
    from proxbox_api.services import netbox_writers
    from proxbox_api.services.sync.vm_create import ensure_vm_platform

    calls: list[str] = []
    monkeypatch.setattr(netbox_writers, "upsert_platform", _counting_upsert(calls))

    cache: dict[str, int | None] = {}
    for ostype in ("l26", "win11", "debian", "l26", "win11"):
        _run(ensure_vm_platform(object(), ostype=ostype, cache=cache))

    assert len(calls) == 3
    assert len(set(calls)) == 3


def test_an_unmapped_ostype_is_not_retried_per_vm(monkeypatch) -> None:
    """A negative result must be cached too, or an unknown guest costs a lookup each time."""
    from proxbox_api.services import netbox_writers
    from proxbox_api.services.sync.vm_create import ensure_vm_platform

    calls: list[str] = []
    monkeypatch.setattr(netbox_writers, "upsert_platform", _counting_upsert(calls))

    cache: dict[str, int | None] = {}
    for _ in range(20):
        assert _run(ensure_vm_platform(object(), ostype="not-a-real-ostype", cache=cache)) is None

    # Nothing to reconcile, and nothing repeatedly attempted.
    assert calls == []


def test_a_transient_netbox_failure_is_not_cached(monkeypatch) -> None:
    """Caching a failure would blank the platform for every remaining VM in the run."""
    from proxbox_api.services import netbox_writers
    from proxbox_api.services.sync.vm_create import ensure_vm_platform

    attempts: list[int] = []

    async def _flaky(nb, *, name, slug, tag_refs=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("NetBox briefly unavailable")
        from types import SimpleNamespace

        class _Record:
            def serialize(self):
                return {"id": 9}

        return SimpleNamespace(record=_Record())

    monkeypatch.setattr(netbox_writers, "upsert_platform", _flaky)

    cache: dict[str, int | None] = {}
    assert _run(ensure_vm_platform(object(), ostype="l26", cache=cache)) is None
    assert _run(ensure_vm_platform(object(), ostype="l26", cache=cache)) == 9


def test_upsert_failure_never_propagates(monkeypatch) -> None:
    """A blank inventory field must not cost a VM its sync."""
    from proxbox_api.services import netbox_writers
    from proxbox_api.services.sync.vm_create import ensure_vm_platform

    async def _boom(nb, *, name, slug, tag_refs=None):
        raise RuntimeError("NetBox rejected the platform")

    monkeypatch.setattr(netbox_writers, "upsert_platform", _boom)
    assert _run(ensure_vm_platform(object(), ostype="l26")) is None


def test_a_malformed_upsert_result_degrades_to_unset(monkeypatch) -> None:
    from types import SimpleNamespace

    from proxbox_api.services import netbox_writers
    from proxbox_api.services.sync.vm_create import ensure_vm_platform

    for record in (
        None,
        SimpleNamespace(serialize=lambda: None),
        SimpleNamespace(serialize=lambda: {}),
    ):

        async def _weird(nb, *, name, slug, tag_refs=None, _record=record):
            return SimpleNamespace(record=_record)

        monkeypatch.setattr(netbox_writers, "upsert_platform", _weird)
        assert _run(ensure_vm_platform(object(), ostype="l26")) is None


# --------------------------------------------------------------------------------------
# Reachability
#
# Both flags are absorbed into FastAPI's grouped query parameter rather than getting
# their own top-level entry, which is how every existing flag on these routes behaves.
# That makes "is the feature reachable at all?" a property worth guarding: a tier-2
# refinement that could never be switched on would be dead code that reads as a feature.
# --------------------------------------------------------------------------------------


def test_the_guest_agent_refinement_can_actually_be_switched_on() -> None:
    from proxbox_api.schemas.sync import behavior_flags_from_query_params

    resolved = behavior_flags_from_query_params(
        {"sync_vm_platform_from_guest_agent": "true"}, SyncBehaviorFlags()
    )
    assert resolved.sync_vm_platform_from_guest_agent is True


def test_an_unknown_overwrite_query_param_cannot_make_platform_patchable() -> None:
    """A stray query parameter must not resurrect the gate by accident."""
    from proxbox_api.schemas.sync import overwrite_flags_from_query_params

    resolved = overwrite_flags_from_query_params(
        {"overwrite_vm_platform": "true"}, SyncOverwriteFlags()
    )
    assert "platform" not in _compute_vm_patchable_fields(resolved)


def test_the_guest_agent_read_is_bounded_by_a_timeout() -> None:
    """A wedged agent must not stall the VM's sync, let alone the stage."""
    import inspect

    from proxbox_api.services import proxmox_helpers

    src = inspect.getsource(proxmox_helpers.get_qemu_guest_agent_osinfo)
    assert "_resolve_guest_agent_timeout()" in src
    assert "asyncio.wait_for" in src
    assert "_scoped_proxmox_backend_timeout" in src


def test_platform_upsert_is_create_only() -> None:
    """An existing platform is referenced, never rewritten.

    A platform named `Ubuntu 22.04` very plausibly predates this sync and carries an
    operator's own name, description, and tags. Patching any of them would be the same
    class of destruction this change exists to avoid, on a record Proxbox merely needs to
    reference. Asserting on the declared `patchable_fields` is the only place this
    property is visible without a live NetBox.
    """
    import inspect

    from proxbox_api.services import netbox_writers

    src = inspect.getsource(netbox_writers.upsert_platform)
    assert "patchable_fields=set()" in src, (
        "the platform upsert must declare an empty patchable set; any non-empty set "
        "lets a sync rewrite a record an operator may own"
    )


def test_platform_upsert_does_not_invent_a_manufacturer() -> None:
    """Proxbox knows the guest OS, not who ships it."""
    import inspect

    from proxbox_api.services import netbox_writers

    src = inspect.getsource(netbox_writers.upsert_platform)
    assert "manufacturer" not in src.split('"""')[2], (
        "a manufacturer would create records the operator never asked for"
    )


def test_documentation_does_not_claim_the_platform_upsert_patches_tags() -> None:
    """The docs described `patchable_fields={"tags"}` after the code became create-only.

    A stale claim in the agent-facing docs is worse than no claim: the next contributor
    reads it as the current contract and reinstates the tag patching this deliberately
    removed. Cheap to assert, and it catches the drift the review found by hand.
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("CLAUDE.md", "AGENTS.md", "docs/sync/overwrite-flags.md"):
        text = (repo_root / name).read_text(encoding="utf-8")
        assert 'patchable_fields={"tags"}' not in text, (
            f"{name} still describes the platform upsert as patching tags; it is create-only"
        )


def test_a_metadata_block_can_pin_the_platform_on_creation() -> None:
    """`platform` is an integer FK, so a `netbox-metadata` fence may override it.

    That is consistent and useful: an operator who wants a specific platform can pin it
    per VM from the Proxmox description.
    """
    from proxbox_api.proxmox_to_netbox.models import _metadata_overridable_fields

    assert "platform" in _metadata_overridable_fields()


def test_a_metadata_pinned_platform_is_still_create_only() -> None:
    """The create-only rule holds regardless of where the value came from.

    A metadata-pinned platform lands when the VM is created, but must not start patching
    existing VMs through the back door -- otherwise the fence becomes an overwrite gate
    that bypasses the one deliberately not added.
    """
    assert "platform" not in _compute_vm_patchable_fields(SyncOverwriteFlags())


def test_the_http_reference_does_not_advertise_a_platform_overwrite_parameter() -> None:
    """It listed `overwrite_vm_platform` as an accepted query parameter.

    That flag has never existed in `SyncOverwriteFlags`, so the reference documented a
    parameter callers could not use — a pre-existing error, and plausibly why this name
    felt like the obvious one to reach for. It is doubly wrong now that the flag's
    absence is a deliberate, tested decision, so the reference is corrected and pinned.
    """
    import pathlib

    reference = (
        pathlib.Path(__file__).resolve().parent.parent / "docs" / "api" / "http-reference.md"
    ).read_text(encoding="utf-8")
    assert "overwrite_vm_platform" not in reference
