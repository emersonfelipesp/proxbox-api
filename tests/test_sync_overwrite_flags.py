"""Tests for the SyncOverwriteFlags Pydantic schema.

Locks in the canonical 24-flag set, the all-True default, and the OpenAPI
metadata (title/description) added so Swagger UI renders the flattened query
parameters correctly. The flag list must stay in lock-step with
`netbox_proxbox.constants.OVERWRITE_FIELDS` on the plugin side.
"""

from __future__ import annotations

import pytest

from proxbox_api.dependencies import resolved_sync_overwrite_flags
from proxbox_api.schemas.sync import SyncOverwriteFlags, overwrite_flags_from_query_params

EXPECTED_FLAGS: tuple[str, ...] = (
    "overwrite_device_role",
    "overwrite_device_type",
    "overwrite_device_tags",
    "overwrite_device_status",
    "overwrite_device_description",
    "overwrite_device_custom_fields",
    "overwrite_vm_role",
    "overwrite_vm_type",
    "overwrite_vm_tags",
    "overwrite_vm_proxmox_tags",
    "overwrite_vm_description",
    "overwrite_vm_custom_fields",
    "overwrite_vm_cloudinit",
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
    "overwrite_ip_address_dns_name",
)


def test_overwrite_flags_field_count() -> None:
    """Schema exposes exactly 24 fields, mirroring OVERWRITE_FIELDS in the plugin."""
    assert len(SyncOverwriteFlags.model_fields) == 24


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


def test_overwrite_flags_from_query_params_makes_flat_query_authoritative() -> None:
    """Raw plugin query params override the model-bound defaults."""
    base = SyncOverwriteFlags(
        overwrite_device_role=True,
        overwrite_device_type=True,
        overwrite_device_tags=True,
    )

    flags = overwrite_flags_from_query_params(
        {
            "overwrite_device_role": "false",
            "overwrite_device_type": "0",
            "overwrite_device_tags": "False",
            "not_an_overwrite_flag": "false",
        },
        base,
    )

    assert flags.overwrite_device_role is False
    assert flags.overwrite_device_type is False
    assert flags.overwrite_device_tags is False
    assert not hasattr(flags, "not_an_overwrite_flag")


def test_resolved_sync_overwrite_flags_dependency_reads_request_query() -> None:
    """Dependency used by routes re-reads the raw flat query string."""

    class _Request:
        query_params = {
            "overwrite_device_role": "false",
            "overwrite_vm_type": "false",
            "overwrite_ip_address_dns_name": "false",
        }

    flags = resolved_sync_overwrite_flags(_Request(), SyncOverwriteFlags())

    assert flags.overwrite_device_role is False
    assert flags.overwrite_vm_type is False
    assert flags.overwrite_ip_address_dns_name is False
