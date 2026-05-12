"""Tests for the opt-in ``netbox-metadata`` description parser."""

from __future__ import annotations

from types import SimpleNamespace

from proxbox_api.proxmox_to_netbox.description_metadata import (
    filter_metadata_by_overwrite_flags,
    parse_netbox_metadata,
    strip_netbox_metadata,
)


def _fence(body: str) -> str:
    return f"```netbox-metadata\n{body}\n```"


def test_parse_returns_pk_map_from_valid_block():
    text = "free text\n" + _fence('{"device": 2, "tenant": 13, "site": 4}') + "\nmore"
    assert parse_netbox_metadata(text) == {"device": 2, "tenant": 13, "site": 4}


def test_parse_empty_or_none_input_returns_empty_dict():
    assert parse_netbox_metadata(None) == {}
    assert parse_netbox_metadata("") == {}
    assert parse_netbox_metadata("just a description, no fence") == {}


def test_parse_malformed_json_returns_empty_dict():
    text = _fence("{not valid json")
    assert parse_netbox_metadata(text) == {}


def test_parse_non_dict_payload_returns_empty_dict():
    assert parse_netbox_metadata(_fence("[1, 2, 3]")) == {}
    assert parse_netbox_metadata(_fence('"just a string"')) == {}
    assert parse_netbox_metadata(_fence("42")) == {}


def test_parse_drops_non_positive_and_non_int_values_but_keeps_valid_keys():
    text = _fence(
        '{"role": 5, "tenant": "13", "site": -1, "device": 0, '
        '"platform": 7, "extra": null, "flag": true}'
    )
    # role and platform are the only positive ints; bool and string/null/zero/negative are dropped.
    assert parse_netbox_metadata(text) == {"role": 5, "platform": 7}


def test_parse_last_block_wins_on_multiple_fences():
    text = (
        "header\n"
        + _fence('{"tenant": 1}')
        + "\nmiddle\n"
        + _fence('{"tenant": 99, "site": 4}')
        + "\nfooter"
    )
    assert parse_netbox_metadata(text) == {"tenant": 99, "site": 4}


def test_parse_fence_is_case_insensitive():
    text = "```NetBox-Metadata\n{\"site\": 4}\n```"
    assert parse_netbox_metadata(text) == {"site": 4}


def test_parse_empty_block_body_returns_empty_dict():
    text = "```netbox-metadata\n\n```"
    assert parse_netbox_metadata(text) == {}


def test_strip_removes_fence_block():
    text = "Production VM\n" + _fence('{"site": 4}') + "\nOwner: ops"
    cleaned = strip_netbox_metadata(text)
    assert cleaned is not None
    assert "netbox-metadata" not in cleaned
    assert "Production VM" in cleaned
    assert "Owner: ops" in cleaned


def test_strip_returns_none_when_only_fence_present():
    text = _fence('{"site": 4}')
    assert strip_netbox_metadata(text) is None


def test_strip_passes_through_none_and_empty():
    assert strip_netbox_metadata(None) is None
    assert strip_netbox_metadata("") == ""


def test_filter_drops_keys_when_overwrite_flag_is_false():
    metadata = {"role": 5, "tenant": 13, "site": 4}
    flags = SimpleNamespace(
        overwrite_vm_role=False,
        overwrite_vm_tenant=True,
        # site has no matching flag; should pass through unconditionally.
    )
    applied, dropped = filter_metadata_by_overwrite_flags(
        metadata, flags, object_kind="vm"
    )
    assert applied == {"tenant": 13, "site": 4}
    assert dropped == ["role"]


def test_filter_keeps_all_when_flags_is_none():
    metadata = {"role": 5, "tenant": 13}
    applied, dropped = filter_metadata_by_overwrite_flags(
        metadata, None, object_kind="vm"
    )
    assert applied == {"role": 5, "tenant": 13}
    assert dropped == []


def test_filter_empty_metadata_returns_empty_tuple():
    flags = SimpleNamespace(overwrite_vm_role=False)
    applied, dropped = filter_metadata_by_overwrite_flags({}, flags, object_kind="vm")
    assert applied == {}
    assert dropped == []


def test_filter_dropped_keys_are_sorted_alphabetically():
    metadata = {"role": 1, "tenant": 2, "platform": 3}
    flags = SimpleNamespace(
        overwrite_vm_role=False,
        overwrite_vm_tenant=False,
        overwrite_vm_platform=False,
    )
    applied, dropped = filter_metadata_by_overwrite_flags(
        metadata, flags, object_kind="vm"
    )
    assert applied == {}
    assert dropped == ["platform", "role", "tenant"]
