"""Fixed feature oracles and failing-input mutations for real route extraction."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from proxbox_api.operation_inventory.collection import check_optional, optional_sequences
from proxbox_api.operation_inventory.generated import identities, load_documents
from proxbox_api.operation_inventory.inputs import load_inputs
from proxbox_api.operation_inventory.schema import InventoryError, load_inventory

ROOT = Path(__file__).absolute().parents[2]
RAW_INPUTS = [
    "",
    "pbs",
    "ceph",
    "pdm",
    "pbs,ceph",
    "pbs,pdm",
    "ceph,pdm",
    "pbs,ceph,pdm",
    "unknown",
    "core,pbs",
    "core,ceph",
    "core,pdm",
    "core,pbs,ceph",
    "core,pbs,pdm",
    "core,ceph,pdm",
    "core,pbs,ceph,pdm",
    " , \t,\n, ",
    " PBS ,pBs,,\tPBS ",
    " CePh, PBS,ceph, ",
    " PDM,CEPH,pbs,PDM, ",
    " Unknown,UNKNOWN, ",
    " cOrE, PBS,pbs,, ",
]
COUNTS = [
    4049,
    3766,
    3786,
    3766,
    3797,
    3777,
    3797,
    3808,
    3996,
    4007,
    4027,
    4007,
    4038,
    4018,
    4038,
    4049,
    4049,
    3766,
    3797,
    3808,
    3996,
    4007,
]


def test_fixed_twenty_two_inputs_and_unreachable_sixteenth_state():
    inputs = load_inputs(ROOT)
    assert [mode.raw for mode in inputs.modes] == RAW_INPUTS
    states = {(mode.core, tuple(mode.sidecars)) for mode in inputs.modes}
    assert len(states) == 15
    assert (False, ()) not in states
    # Empty selects all; unknown selects core. Neither expresses an empty service.
    assert inputs.modes[0].core is True
    assert inputs.modes[0].sidecars == ["pbs", "ceph", "pdm"]
    assert inputs.modes[8].core is True
    assert inputs.modes[8].sidecars == []
    assert inputs.generated_versions == ["latest", "8.1", "8.2", "8.3", "9.1"]
    assert [(row.module, row.prefix) for row in inputs.optional_routers] == [
        ("proxbox_api.pbs.admin", "/pbs"),
        ("proxbox_api.pbs.routes", "/pbs"),
        ("proxbox_api.ceph.routes", "/ceph"),
        ("proxbox_api.ceph.v2_routes", "/ceph/v2"),
        ("proxbox_api.pdm.admin", "/pdm"),
        ("proxbox_api.pdm.routes", "/pdm"),
    ]


def test_missing_manifest_is_an_error(tmp_path):
    with pytest.raises(InventoryError):
        load_inputs(tmp_path)


def test_missing_optional_extra_is_an_error(monkeypatch):
    def absent(name):
        raise ModuleNotFoundError("synthetic absent optional extra")

    monkeypatch.setattr(
        "proxbox_api.operation_inventory.collection.importlib.import_module", absent
    )
    with pytest.raises(ModuleNotFoundError):
        optional_sequences(load_inputs(ROOT), ROOT)


def test_missing_optional_registrations_are_an_error():
    inputs = load_inputs(ROOT)
    expected = optional_sequences(inputs, ROOT)
    with pytest.raises(InventoryError, match="sequence differs"):
        check_optional([], inputs.modes[0], expected)
    check_optional([], inputs.modes[8], expected)


def test_generated_versions_and_missing_schema_fail_closed(tmp_path):
    with pytest.raises(InventoryError, match="version set"):
        load_documents(tmp_path, ["latest"])
    directory = tmp_path / "proxbox_api/generated/proxmox/latest"
    directory.mkdir(parents=True)
    (directory / "openapi.json").write_text('{"paths":{}}')
    with pytest.raises(InventoryError, match="missing paths"):
        load_documents(tmp_path, ["latest"])


@pytest.mark.parametrize(
    "paths",
    [[], [1], {"relative": {}}, {"/probe": []}, {"/probe": {"get": []}}, {"/probe": {"get": {}}}],
)
def test_malformed_nested_generated_schema_fails(tmp_path, paths):
    directory = tmp_path / "proxbox_api/generated/proxmox/latest"
    directory.mkdir(parents=True)
    document = {"info": {"version": "fixture"}, "paths": paths}
    (directory / "openapi.json").write_text(json.dumps(document))
    with pytest.raises(InventoryError):
        identities(tmp_path, load_documents(tmp_path, ["latest"]))


def _included(row, mode):
    for feature in ("pbs", "ceph", "pdm"):
        if row.path.startswith("/" + feature + "/"):
            return feature in mode.sidecars
    if row.generated:
        return True
    if row.handler.module in {
        "fastapi.applications",
        "proxbox_api.app.root_meta",
        "proxbox_api.routes.auth",
    }:
        return True
    if row.name in {"static", "custom_swagger_ui", "custom_redoc"}:
        return True
    return mode.core


def assert_all_modes(inventory):
    assert [mode.feature_tokens for mode in inventory.modes] == RAW_INPUTS
    assert [len(mode.registrations) for mode in inventory.modes] == COUNTS
    default = [inventory.operations[row.operation] for row in inventory.modes[0].registrations]
    generated = [row for row in default if row.generated]
    assert len(generated) == 3741
    assert [(row.path, row.methods) for row in generated[:2]] == [
        ("/proxmox/api2/latest/access", ["GET"]),
        ("/proxmox/api2/access", ["GET"]),
    ]
    for mode in inventory.modes:
        rows = [inventory.operations[row.operation] for row in mode.registrations]
        assert rows == [row for row in default if _included(row, mode)]
        assert [row for row in rows if row.generated] == generated
        sockets = [row.path for row in rows if row.protocol == "websocket"]
        assert sockets == (
            ["/", "/ws/virtual-machines", "/ws", "/ssh/sessions/{session_id}/ws"]
            if mode.core
            else []
        )


def test_committed_real_inventory_all_mode_oracles():
    # Missing artifact is a failure, never a skip or regenerated expectation.
    inventory = load_inventory((ROOT / "contracts/mounted-operations.json").read_bytes())
    assert_all_modes(inventory)


def test_removed_registration_breaks_fixed_mode_oracle():
    inventory = load_inventory((ROOT / "contracts/mounted-operations.json").read_bytes())
    changed = copy.deepcopy(inventory)
    changed.modes[0].registrations.pop()
    with pytest.raises(AssertionError):
        assert_all_modes(changed)
