"""Small real generated-route composition and explicit failure propagation."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from proxbox_api.operation_inventory import collection
from proxbox_api.operation_inventory.generated import bind, identities, load_documents
from proxbox_api.operation_inventory.inputs import load_inputs
from proxbox_api.operation_inventory.schema import InventoryError

ROOT = Path(__file__).absolute().parents[2]


@pytest.fixture
def tiny_documents(tmp_path):
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Inventory fixture", "version": "fixture"},
        "paths": {
            "/inventory-probe": {
                "get": {
                    "operationId": "get_inventory_probe",
                    "responses": {"200": {"description": "ok"}},
                },
                "parameters": [],
            },
        },
    }
    for version in ("latest", "8.3"):
        path = tmp_path / f"proxbox_api/generated/proxmox/{version}/openapi.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document))
    documents = load_documents(tmp_path, ["latest", "8.3"])
    return documents, identities(tmp_path, documents)


def test_real_generated_registration_and_independent_explicit_alias_sequence(tiny_documents):
    documents, expected = tiny_documents
    inputs = load_inputs(ROOT)
    optional = collection.optional_sequences(inputs, ROOT)
    rows = collection._collect_mode(ROOT, inputs.modes[8], documents, expected, optional)
    actual = [(row.path, row.methods, row.generated.alias) for row in rows if row.generated]
    assert actual == [
        ("/proxmox/api2/latest/inventory-probe", ["GET"], False),
        ("/proxmox/api2/inventory-probe", ["GET"], True),
        ("/proxmox/api2/8.3/inventory-probe", ["GET"], False),
    ]
    assert [row.path for row in rows if row.protocol == "websocket"] == [
        "/",
        "/ws/virtual-machines",
        "/ws",
        "/ssh/sessions/{session_id}/ws",
    ]
    collection.check_core([], inputs.modes[1])
    with pytest.raises(InventoryError, match="WebSocket"):
        collection.check_core([], inputs.modes[8])


@pytest.mark.parametrize("mutation", ["omission", "count", "failure"])
def test_registration_failure_or_partial_result_never_becomes_inventory(
    tiny_documents, monkeypatch, mutation
):
    from proxbox_api.routes.proxmox import runtime_generated

    documents, expected = tiny_documents
    actual = runtime_generated.register_generated_proxmox_routes

    def incomplete(app, *, openapi_documents):
        if mutation == "failure":
            raise ValueError("Synthetic generated schema failure")
        state = actual(app, openapi_documents=openapi_documents)
        if mutation == "count":
            return {**state, "route_count": 0}
        name = "generated_proxmox_route__latest__get__get_inventory_probe"
        original = len(app.router.routes)
        app.router.routes = [row for row in app.router.routes if getattr(row, "name", None) != name]
        assert len(app.router.routes) == original - 1
        return state

    monkeypatch.setattr(runtime_generated, "register_generated_proxmox_routes", incomplete)
    inputs = load_inputs(ROOT)
    optional = collection.optional_sequences(inputs, ROOT)
    with pytest.raises((ValueError, InventoryError)):
        collection._collect_mode(ROOT, inputs.modes[8], documents, expected, optional)


@pytest.mark.parametrize("mutation", ["name", "method", "version", "path"])
def test_generated_identity_mutations_fail(tiny_documents, minimal, mutation):
    from proxbox_api.operation_inventory.schema import Operation

    _, expected = tiny_documents
    wire = copy.deepcopy(next(iter(minimal["operations"].values())))
    wire.update(
        name="generated_proxmox_route__latest__get__get_inventory_probe",
        path="/proxmox/api2/latest/inventory-probe",
    )
    operation = Operation.model_validate(wire)
    assert bind(operation, expected).generated.upstream_path == "/inventory-probe"
    changes = {
        "name": {"name": "unknown"},
        "method": {"methods": ["DELETE"]},
        "version": {"path": "/proxmox/api2/8.2/inventory-probe"},
        "path": {"path": "/proxmox/api2/latest/different"},
    }
    with pytest.raises(InventoryError):
        bind(operation.model_copy(update=changes[mutation]), expected)


def test_duplicate_generated_operation_ids_fail(tiny_documents, tmp_path):
    documents, _ = tiny_documents
    documents["latest"]["paths"]["/another"] = copy.deepcopy(
        documents["latest"]["paths"]["/inventory-probe"]
    )
    with pytest.raises(InventoryError, match="collide"):
        identities(tmp_path, documents)


@pytest.mark.parametrize(
    "change", [{"alias": True, "version": "8.3"}, {"upstream_path": "relative"}]
)
def test_generated_wire_alias_and_path_invariants(tiny_documents, change):
    from proxbox_api.operation_inventory.schema import Generated

    wire = next(iter(tiny_documents[1].values())).model_dump()
    wire.update(change)
    with pytest.raises(ValueError):
        Generated.model_validate(wire)


def test_generated_wire_method_must_match_registration(tiny_documents, minimal):
    from proxbox_api.operation_inventory.schema import Operation

    wire = next(iter(minimal["operations"].values()))
    wire["generated"] = next(iter(tiny_documents[1].values())).model_dump()
    wire["methods"] = ["DELETE"]
    with pytest.raises(ValueError, match="Generated method"):
        Operation.model_validate(wire)


@pytest.mark.parametrize("mutation", ["mode", "optional", "version", "coercion"])
def test_incomplete_or_coerced_input_manifest_fails(tmp_path, mutation):
    value = json.loads((ROOT / "contracts/operation-inventory-inputs.json").read_bytes())
    if mutation == "coercion":
        value["schema_version"] = True
    else:
        key = {"mode": "modes", "optional": "optional_routers", "version": "generated_versions"}[
            mutation
        ]
        value[key] = []
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts/operation-inventory-inputs.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_inputs(tmp_path)


@pytest.mark.parametrize("mutate_equivalent", [False, True])
def test_real_collect_orchestration_with_bounded_schema(
    tiny_documents, monkeypatch, mutate_equivalent
):
    documents, _ = tiny_documents
    inputs = load_inputs(ROOT)
    bounded = inputs.model_copy(update={"modes": [inputs.modes[8], inputs.modes[20]]})
    monkeypatch.setattr(collection, "load_inputs", lambda _: bounded)
    monkeypatch.setattr(collection, "load_documents", lambda root, versions: documents)
    # Keep identity schema provenance on the actual fixture files rather than claiming bundled bytes.
    expected = tiny_documents[1]
    monkeypatch.setattr(collection, "identities", lambda root, documents: expected)
    actual = collection._collect_mode

    def mode_rows(root, mode, docs, generated, optional):
        rows = actual(root, mode, docs, generated, optional)
        return rows[1:] if mutate_equivalent and mode.equivalent else rows

    monkeypatch.setattr(collection, "_collect_mode", mode_rows)
    if mutate_equivalent:
        with pytest.raises(InventoryError, match="normalization"):
            collection.collect(ROOT)
    else:
        inventory = collection.collect(ROOT)
        assert len(inventory.modes) == 2
        assert inventory.modes[0].registrations == inventory.modes[1].registrations
        assert len(inventory.modes[0].registrations) == 258
        assert inventory.provenance.imported_modules


@pytest.mark.parametrize("mutation", ["unreviewed", "empty"])
def test_optional_manifest_and_empty_router_refusal(monkeypatch, mutation):
    from types import SimpleNamespace

    from fastapi import APIRouter

    inputs = load_inputs(ROOT)
    if mutation == "unreviewed":
        inputs = inputs.model_copy(update={"optional_routers": []})
    else:
        monkeypatch.setattr(
            collection.importlib, "import_module", lambda _: SimpleNamespace(router=APIRouter())
        )
    with pytest.raises(InventoryError):
        collection.optional_sequences(inputs, ROOT)
