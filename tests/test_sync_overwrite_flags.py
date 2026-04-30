"""Tests for the SyncOverwriteFlags Pydantic schema.

Locks in the canonical 21-flag set, the all-True default, and the OpenAPI
metadata (title/description) added so Swagger UI renders the flattened query
parameters correctly. The flag list must stay in lock-step with
`netbox_proxbox.constants.OVERWRITE_FIELDS` on the plugin side.
"""

from __future__ import annotations

import pytest

from proxbox_api.schemas.sync import SyncOverwriteFlags

EXPECTED_FLAGS: tuple[str, ...] = (
    "overwrite_device_role",
    "overwrite_device_type",
    "overwrite_device_tags",
    "overwrite_device_status",
    "overwrite_device_description",
    "overwrite_device_custom_fields",
    "overwrite_vm_role",
    "overwrite_vm_tags",
    "overwrite_vm_description",
    "overwrite_vm_custom_fields",
    "overwrite_cluster_tags",
    "overwrite_cluster_description",
    "overwrite_cluster_custom_fields",
    "overwrite_node_interface_tags",
    "overwrite_node_interface_custom_fields",
    "overwrite_storage_tags",
    "overwrite_vm_interface_tags",
    "overwrite_vm_interface_custom_fields",
    "overwrite_ip_status",
    "overwrite_ip_tags",
    "overwrite_ip_custom_fields",
)


def test_overwrite_flags_field_count() -> None:
    """Schema exposes exactly 21 fields, mirroring OVERWRITE_FIELDS in the plugin."""
    assert len(SyncOverwriteFlags.model_fields) == 21


def test_overwrite_flags_field_names_and_order() -> None:
    """Field names and declaration order match the canonical contract."""
    assert tuple(SyncOverwriteFlags.model_fields.keys()) == EXPECTED_FLAGS


def test_overwrite_flags_all_default_true() -> None:
    """Every flag defaults to True (preserve historical always-overwrite semantics)."""
    flags = SyncOverwriteFlags()
    for name in EXPECTED_FLAGS:
        assert getattr(flags, name) is True, f"{name} should default to True"


def test_overwrite_flags_all_bool_type() -> None:
    """Every field is annotated as `bool`."""
    for name, field in SyncOverwriteFlags.model_fields.items():
        assert field.annotation is bool, f"{name} should be typed as bool"


def test_overwrite_flags_have_title_and_description() -> None:
    """Each flag carries OpenAPI title + description so /docs renders properly."""
    for name, field in SyncOverwriteFlags.model_fields.items():
        assert field.title, f"{name} missing OpenAPI title"
        assert field.description, f"{name} missing OpenAPI description"


@pytest.mark.parametrize("flag_name", EXPECTED_FLAGS)
def test_overwrite_flags_individually_settable_to_false(flag_name: str) -> None:
    """Each flag can be flipped to False without affecting the others."""
    flags = SyncOverwriteFlags(**{flag_name: False})
    for other in EXPECTED_FLAGS:
        expected = False if other == flag_name else True
        assert getattr(flags, other) is expected
