"""Regression tests: a Proxmox VM note must survive the sync.

Every sync used to overwrite the NetBox ``description`` with the literal
``Synced from Proxmox node {node}`` placeholder, on every run rather than only at
creation, so an operator maintaining or restoring VM notes in NetBox lost them again on
the next sync.

Three payload builders were defective in two different shapes:

1. ``proxmox_to_netbox/models.py::as_netbox_create_body()`` hardcoded the placeholder in
   the ``parse_description_metadata=False`` branch -- the default;
2. ``services/sync/individual/vm_sync.py::_build_netbox_vm_payload()`` hardcoded it
   unconditionally, with no flag-on branch at all;
3. ``services/sync/vm_create.py`` never passed ``parse_description_metadata``, so it took
   the placeholder branch regardless of the operator's setting.

The tests below therefore assert against **all three** builders, not only the transform
they happen to share, because sharing is precisely what was missing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from proxbox_api.proxmox_to_netbox.description_metadata import (
    NETBOX_DESCRIPTION_MAX_CHARS,
    derive_description_and_comments,
)
from proxbox_api.proxmox_to_netbox.mappers.virtual_machine import (
    map_proxmox_vm_to_netbox_vm_body,
)
from proxbox_api.proxmox_to_netbox.models import _metadata_overridable_fields
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.individual.vm_sync import _build_netbox_vm_payload
from proxbox_api.services.sync.virtual_machines import build_netbox_virtual_machine_payload
from proxbox_api.services.sync.vm_helpers import (
    _compute_vm_patchable_fields,
    normalize_current_virtual_machine_payload,
)

PLACEHOLDER = "Synced from Proxmox node pve01"

METADATA_FENCE = '```netbox-metadata\n{"tenant": 13}\n```'

RESOURCE = {
    "name": "vm1",
    "node": "pve01",
    "status": "running",
    "maxcpu": 2,
    "vmid": 100,
    "type": "qemu",
    "maxmem": 1024**3,
    "maxdisk": 10 * 1024**3,
}


def _bulk(config: dict, *, parse_metadata: bool = False) -> dict:
    """Payload from the bulk ``virtual-machines`` stage builder."""
    return map_proxmox_vm_to_netbox_vm_body(
        RESOURCE,
        config,
        cluster_id=1,
        device_id=1,
        role_id=1,
        tag_ids=[1],
        parse_description_metadata=parse_metadata,
    )


def _individual(config: dict) -> dict:
    """Payload from the per-VM sync builder."""
    return _build_netbox_vm_payload(
        dict(RESOURCE),
        config,
        cluster_id=1,
        device_id=1,
        role_id=1,
        tag_ids=[1],
        last_updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _service(config: dict) -> dict:
    """Payload from ``services/sync/virtual_machines.py``.

    This is the entry point ``services/sync/vm_create.py`` calls, and it called it
    *without* ``parse_description_metadata`` -- which is why that path took the
    placeholder branch no matter what the operator configured. Exercised with the flag
    omitted, exactly as vm_create.py calls it.
    """
    return build_netbox_virtual_machine_payload(
        proxmox_resource=RESOURCE,
        proxmox_config=config,
        cluster_id=1,
        device_id=1,
        role_id=1,
        tag_ids=[1],
    )


# All three builders must agree for every input; parametrizing over them is what makes a
# regression in only one of them visible. They diverged before precisely because each had
# its own copy of the rule.
BUILDERS = {"bulk-stage": _bulk, "individual": _individual, "vm-create-service": _service}


@pytest.mark.parametrize("builder_name", sorted(BUILDERS))
def test_absent_note_falls_back_to_the_placeholder(builder_name: str) -> None:
    payload = BUILDERS[builder_name]({})
    assert payload["description"] == PLACEHOLDER
    assert "comments" not in payload or payload["comments"] is None


@pytest.mark.parametrize("builder_name", sorted(BUILDERS))
def test_blank_note_falls_back_to_the_placeholder(builder_name: str) -> None:
    payload = BUILDERS[builder_name]({"description": "   \n\t  \n "})
    assert payload["description"] == PLACEHOLDER


@pytest.mark.parametrize("builder_name", sorted(BUILDERS))
def test_single_line_note_becomes_the_description(builder_name: str) -> None:
    payload = BUILDERS[builder_name]({"description": "primary web server"})
    assert payload["description"] == "primary web server"
    # A one-line note fits entirely in the description; duplicating it into comments
    # would just be noise.
    assert payload.get("comments") is None


@pytest.mark.parametrize("builder_name", sorted(BUILDERS))
def test_multi_line_note_keeps_the_first_line_and_stores_the_whole_note(
    builder_name: str,
) -> None:
    note = "primary web server\nowner: platform team\nticket: OPS-42"
    payload = BUILDERS[builder_name]({"description": note})
    assert payload["description"] == "primary web server"
    assert payload["comments"] == note


@pytest.mark.parametrize("builder_name", sorted(BUILDERS))
def test_over_length_note_is_truncated_visibly_and_preserved_in_comments(
    builder_name: str,
) -> None:
    note = "a" * (NETBOX_DESCRIPTION_MAX_CHARS + 50)
    payload = BUILDERS[builder_name]({"description": note})
    description = payload["description"]
    # NetBox rejects anything longer, so the cap is a hard requirement, not a style choice.
    assert len(description) == NETBOX_DESCRIPTION_MAX_CHARS
    assert description != note[:NETBOX_DESCRIPTION_MAX_CHARS], (
        "a silently clipped description reads as a complete one"
    )
    # Nothing is lost: the untruncated note is still there.
    assert payload["comments"] == note


@pytest.mark.parametrize("builder_name", sorted(BUILDERS))
def test_note_that_is_only_a_metadata_fence_falls_back_to_the_placeholder(
    builder_name: str,
) -> None:
    payload = BUILDERS[builder_name]({"description": METADATA_FENCE})
    assert payload["description"] == PLACEHOLDER
    assert payload.get("comments") is None


@pytest.mark.parametrize("builder_name", sorted(BUILDERS))
def test_metadata_fence_is_stripped_from_the_written_note(builder_name: str) -> None:
    """Stripping is unconditional; the toggle governs PK overrides, not the text.

    Before the note was used at all, an unstripped fence leaked nowhere. Now that it is
    written to NetBox, leaving it in would put raw JSON into the rendered UI.
    """
    payload = BUILDERS[builder_name]({"description": f"real note\n{METADATA_FENCE}"})
    assert payload["description"] == "real note"
    assert "netbox-metadata" not in (payload.get("comments") or "")
    assert "netbox-metadata" not in payload["description"]


def test_bulk_builder_strips_the_fence_with_metadata_parsing_enabled_too() -> None:
    payload = _bulk({"description": f"real note\n{METADATA_FENCE}"}, parse_metadata=True)
    assert payload["description"] == "real note"
    assert "netbox-metadata" not in payload["description"]


def test_note_preservation_does_not_depend_on_the_metadata_toggle() -> None:
    """The property the report is really about: the flags are unrelated concerns."""
    note = "primary web server\nowner: platform team"
    off = _bulk({"description": note}, parse_metadata=False)
    on = _bulk({"description": note}, parse_metadata=True)
    assert off["description"] == on["description"] == "primary web server"
    assert off["comments"] == on["comments"] == note


def test_crlf_note_does_not_leave_a_stray_carriage_return() -> None:
    description, comments = derive_description_and_comments(
        "line one\r\nline two", fallback=PLACEHOLDER
    )
    assert description == "line one"
    assert "\r" not in (comments or "")


def test_non_string_note_falls_back_without_raising() -> None:
    """Proxmox config values arrive untyped; a non-string must degrade, not throw."""
    for hostile in (None, 42, {"nested": "object"}, ["a", "b"], object()):
        assert derive_description_and_comments(hostile, fallback=PLACEHOLDER) == (
            PLACEHOLDER,
            None,
        )


# --------------------------------------------------------------------------------------
# Reconciler wiring
# --------------------------------------------------------------------------------------


def test_comments_rides_the_description_overwrite_gate() -> None:
    """Both fields carry the same operator-authored content under the same consent."""
    permissive = _compute_vm_patchable_fields(SyncOverwriteFlags())
    assert {"description", "comments"} <= permissive

    locked = _compute_vm_patchable_fields(SyncOverwriteFlags(overwrite_vm_description=False))
    assert "description" not in locked
    assert "comments" not in locked, (
        "leaving comments patchable would overwrite the note the operator locked"
    )


def test_default_flags_still_patch_both_fields() -> None:
    assert {"description", "comments"} <= _compute_vm_patchable_fields(None)


def test_reconciler_diff_can_see_the_current_comments_value() -> None:
    """Without this the field would be write-once at creation and never patched."""
    normalized = normalize_current_virtual_machine_payload(
        {"name": "vm1", "description": "old", "comments": "old full note"}
    )
    assert normalized["comments"] == "old full note"


# --------------------------------------------------------------------------------------
# netbox-metadata override surface
#
# Adding ``comments`` to the create body widened which keys a fenced block could
# override. The block carries NetBox primary-key *integers*, so assigning one to a text
# field raises a ValidationError that fails the whole VM's sync -- already reachable
# through ``description`` before this change, and it would have widened to ``comments``.
# --------------------------------------------------------------------------------------


def test_metadata_cannot_override_text_fields() -> None:
    overridable = _metadata_overridable_fields()
    assert "description" not in overridable
    assert "comments" not in overridable
    assert "name" not in overridable
    assert "status" not in overridable


def test_metadata_cannot_override_container_fields() -> None:
    """``list[int]`` also contains ``int``; a bare integer would still be rejected."""
    assert "tags" not in _metadata_overridable_fields()
    assert "custom_fields" not in _metadata_overridable_fields()


def test_metadata_can_still_override_the_foreign_keys_it_is_for() -> None:
    overridable = _metadata_overridable_fields()
    assert {"site", "device", "cluster", "role", "virtual_machine_type"} <= overridable


@pytest.mark.parametrize("key", ["comments", "description", "tags"])
def test_metadata_override_of_a_non_integer_field_is_dropped_not_fatal(key: str) -> None:
    """The whole point: a hostile or mistaken block must not fail the VM's sync."""
    note = f'real note\n```netbox-metadata\n{{"{key}": 5}}\n```'
    payload = _bulk({"description": note}, parse_metadata=True)
    assert payload["description"] == "real note"
    assert payload.get(key) != 5


@pytest.mark.parametrize("builder_name", sorted(BUILDERS))
def test_comments_is_omitted_rather_than_cleared(builder_name: str) -> None:
    """Omission means "do not write", deliberately not "clear".

    Emitting an empty string would wipe comments an operator wrote by hand on a VM that
    never had a Proxmox note -- the exact destruction this change exists to stop. The
    cost is accepted and documented: shortening a multi-line note to one line leaves the
    previous full note in NetBox until it is edited there.
    """
    payload = BUILDERS[builder_name]({"description": "one line only"})
    assert payload.get("comments") is None
    assert payload.get("comments") != "", (
        "an empty string would clear operator-written comments; omit the key instead"
    )
