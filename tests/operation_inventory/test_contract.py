"""Independent hostile-input and fixed digest oracles for registration records."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from proxbox_api.operation_inventory.coverage import check_coverage, unresolved
from proxbox_api.operation_inventory.schema import (
    InventoryError,
    canonical,
    digest,
    load_coverage,
    load_inventory,
    parse,
)

ROOT = Path(__file__).absolute().parents[2]


@pytest.fixture
def minimal():
    operation = {
        "protocol": "http",
        "path": "/fixed/{item}",
        "declared_path": "/fixed/{item}",
        "methods": ["GET"],
        "route_kind": "APIRoute",
        "name": "fixed",
        "mounts": [],
        "handler": {
            "module": "fixture",
            "qualname": "fixed",
            "line": 1,
            "source": {"owner": "repository", "path": "fixture.py", "sha256": "1" * 64},
        },
        "generated": None,
    }
    key = digest(operation)
    return {
        "schema_version": 1,
        "operations": {key: operation},
        "provenance": {
            "python": "3.12.13",
            "dependencies": [],
            "sources": [],
            "framework_sources": [],
            "imported_modules": {},
        },
        "modes": [
            {
                "name": "fixture",
                "feature_tokens": "core",
                "core": True,
                "sidecars": [],
                "registrations": [{"index": 0, "operation": key}, {"index": 1, "operation": key}],
            }
        ],
    }


def test_fixed_canonical_digest():
    assert canonical({"a": 1}) == b'{"a":1}\n'
    # Independently measured with printf plus sha256sum, not implementation constants.
    assert digest({"a": 1}) == "e346432021b04179518d9614f3560ccd71354a4ee101ddcb893d6959a9d6301c"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":{"b":1,"b":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":"\\u0000"}',
        b'{"x":"\\ud800"}',
        b"\xff",
        b"{",
    ],
)
def test_hostile_raw_documents_fail(raw):
    with pytest.raises((InventoryError, UnicodeError)):
        parse(raw)


@pytest.mark.parametrize("value", [1.0, True, "1", 2, None])
def test_schema_version_aliases_fail(minimal, value):
    minimal["schema_version"] = value
    with pytest.raises((InventoryError, ValidationError)):
        load_inventory(canonical(minimal))


@pytest.mark.parametrize(
    "path,value",
    [
        (("modes", 0, "registrations", 0, "index"), True),
        (("modes", 0, "registrations", 0, "index"), 0.0),
        (("modes", 0, "core"), 1),
        (("modes", 0, "registrations", 1, "index"), 0),
        (("modes", 0, "registrations", 0, "operation"), "f" * 64),
        (("provenance", "sources"), "not-a-list"),
    ],
)
def test_nested_record_mutations_fail(minimal, path, value):
    node = minimal
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises((InventoryError, ValidationError)):
        load_inventory(canonical(minimal))


def test_duplicate_registrations_are_preserved(minimal):
    inventory = load_inventory(canonical(minimal))
    assert len(inventory.modes[0].registrations) == 2
    assert (
        inventory.modes[0].registrations[0].operation
        == inventory.modes[0].registrations[1].operation
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", "websocket"),
        ("methods", []),
        ("methods", ["GET", "GET"]),
        ("methods", ["get"]),
        ("path", "relative"),
        ("surprise", "bad"),
    ],
)
def test_impossible_operation_mutations_fail(minimal, field, value):
    operation = next(iter(minimal["operations"].values()))
    operation[field] = value
    with pytest.raises((InventoryError, ValidationError)):
        load_inventory(canonical(minimal))


def test_unresolved_coverage_is_never_ready(minimal):
    inventory = load_inventory(canonical(minimal))
    coverage = unresolved(inventory)
    assert len(check_coverage(inventory, coverage)) == 16
    assert coverage.records[0].effects == []
    wire = coverage.model_dump()
    assert load_coverage(canonical(wire)) == coverage
    wire["records"][0]["callers"]["status"] = "evidenced"
    with pytest.raises(ValidationError):
        load_coverage(canonical(wire))


def test_coverage_requires_exact_complete_join(minimal):
    inventory = load_inventory(canonical(minimal))
    wire = unresolved(inventory).model_dump()
    wire["records"].append(copy.deepcopy(wire["records"][0]))
    with pytest.raises(InventoryError):
        check_coverage(inventory, load_coverage(canonical(wire)))
    wire["records"] = []
    with pytest.raises(InventoryError):
        check_coverage(inventory, load_coverage(canonical(wire)))


def test_structural_and_byte_bounds(monkeypatch):
    from proxbox_api.operation_inventory import schema

    with pytest.raises(InventoryError):
        canonical({1: "non-string key"})
    with pytest.raises(InventoryError):
        canonical({"x": object()})
    value = []
    for _ in range(50):
        value = [value]
    with pytest.raises(InventoryError):
        canonical(value)
    monkeypatch.setattr(schema, "MAX_BYTES", 4)
    with pytest.raises(InventoryError):
        parse(b'"long"')
    with pytest.raises(InventoryError):
        canonical("long")
    monkeypatch.setattr(schema, "MAX_NODES", 1)
    with pytest.raises(InventoryError):
        schema.check_shape([1])


@pytest.mark.parametrize("path", ["/absolute.py", "../parent.py", "bad\\path.py", "bad//path.py"])
def test_source_paths_are_canonical_relative(path):
    from proxbox_api.operation_inventory.schema import Source

    with pytest.raises(ValidationError):
        Source(owner="repository", path=path, sha256="1" * 64)


@pytest.mark.parametrize("mutation", ["empty", "names", "digest"])
def test_inventory_complete_keys_and_digests(minimal, mutation):
    if mutation == "empty":
        minimal["modes"] = []
    elif mutation == "names":
        minimal["modes"].append(copy.deepcopy(minimal["modes"][0]))
    else:
        next(iter(minimal["operations"].values()))["name"] = "changed"
    with pytest.raises(ValidationError):
        load_inventory(canonical(minimal))


@pytest.mark.parametrize(
    "effects", [["managed_read", "managed_read"], ["no_external_effect", "managed_mutation"], []]
)
def test_effect_disposition_never_accepts_contradictory_or_empty_evidence(minimal, effects):
    wire = unresolved(load_inventory(canonical(minimal))).model_dump()
    row = wire["records"][0]
    row["effects"] = effects
    row["effect_evidence"].update(status="evidenced", citations=["fixture.py:1"])
    with pytest.raises(ValidationError):
        load_coverage(canonical(wire))


def test_coverage_schema_version_coercion_fails(minimal):
    wire = unresolved(load_inventory(canonical(minimal))).model_dump()
    wire["schema_version"] = 1.0
    with pytest.raises(InventoryError):
        load_coverage(canonical(wire))
