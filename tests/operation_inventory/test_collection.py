"""Fixed feature oracles and failing-input mutations for real route extraction."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from proxbox_api.app import factory
from proxbox_api.operation_inventory.adapter import walk
from proxbox_api.operation_inventory.collection import check_optional, optional_sequences
from proxbox_api.operation_inventory.generated import bind, identities, load_documents
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
    4053,
    3767,
    3789,
    3767,
    3800,
    3778,
    3800,
    3811,
    3998,
    4009,
    4031,
    4009,
    4042,
    4020,
    4042,
    4053,
    4053,
    3767,
    3800,
    3811,
    3998,
    4009,
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


def _registered_rows(inventory, registrations):
    return [inventory.operations[row.operation] for row in registrations]


def _assert_core_route_oracles(rows, mode):
    sockets = [row.path for row in rows if row.protocol == "websocket"]
    assert sockets == (
        [
            "/",
            "/ws/virtual-machines",
            "/ws",
            "/proxmox/console/browser-stream",
            "/ssh/sessions/{session_id}/ws",
        ]
        if mode.core
        else []
    )
    standalone = [
        (row.path, row.methods, row.protocol)
        for row in rows
        if row.name in {"create_browser_console_session", "browser_console_stream"}
    ]
    assert standalone == (
        [
            ("/proxmox/console/browser-sessions", ["POST"], "http"),
            ("/proxmox/console/browser-stream", [], "websocket"),
        ]
        if mode.core
        else []
    )


def _assert_generated_sequence(rows):
    generated = [row for row in rows if row.generated]
    assert len(generated) == 3741
    assert [(row.path, row.methods) for row in generated[:2]] == [
        ("/proxmox/api2/latest/access", ["GET"]),
        ("/proxmox/api2/access", ["GET"]),
    ]
    return generated


def _assert_mode_rows(inventory, mode, default, generated):
    rows = _registered_rows(inventory, mode.registrations)
    assert rows == [row for row in default if _included(row, mode)]
    assert [row for row in rows if row.generated] == generated
    _assert_core_route_oracles(rows, mode)


def assert_all_modes(inventory):
    assert [mode.feature_tokens for mode in inventory.modes] == RAW_INPUTS
    assert [len(mode.registrations) for mode in inventory.modes] == COUNTS
    default = _registered_rows(inventory, inventory.modes[0].registrations)
    generated = _assert_generated_sequence(default)
    for mode in inventory.modes:
        _assert_mode_rows(inventory, mode, default, generated)
    opt_in = _registered_rows(inventory, inventory.runtime_codegen_opt_in.registrations)
    _assert_generated_sequence(opt_in)
    assert len(inventory.runtime_codegen_opt_in.registrations) == 4055


def test_committed_real_inventory_all_mode_oracles():
    # Missing artifact is a failure, never a skip or regenerated expectation.
    inventory = load_inventory((ROOT / "contracts/mounted-operations.json").read_bytes())
    assert_all_modes(inventory)


@pytest.mark.asyncio
async def test_default_lifespan_mounts_committed_default_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from proxbox_api.routes.proxmox import runtime_generated

    async def skip_bootstrap(_app: FastAPI) -> None:
        return None

    async def skip_dispose() -> None:
        return None

    monkeypatch.delenv("PROXBOX_RUNTIME_CODEGEN_ENABLED", raising=False)
    monkeypatch.delenv("PROXBOX_FEATURES", raising=False)
    monkeypatch.setattr(factory.bootstrap, "init_database_and_netbox", lambda _owner: None)
    monkeypatch.setattr(factory, "validate_auth_lockout_identity_key", lambda: None)
    monkeypatch.setattr(factory, "quarantine_legacy_codegen_artifacts", lambda: [])
    monkeypatch.setattr(factory, "_run_bootstrap_pass", skip_bootstrap)
    monkeypatch.setattr(factory.database, "dispose_database", skip_dispose)
    monkeypatch.setattr(
        runtime_generated,
        "_generated_route_cache_path",
        lambda: tmp_path / "runtime_generated_routes_cache.json",
    )

    application = factory.create_app()
    async with factory._lifespan(application):
        inputs = load_inputs(ROOT)
        documents = load_documents(ROOT, inputs.generated_versions)
        generated = identities(ROOT, documents)
        actual = [bind(row, generated) for row in walk(application.routes, ROOT)]

    inventory = load_inventory((ROOT / "contracts/mounted-operations.json").read_bytes())
    expected = [
        inventory.operations[registration.operation]
        for registration in inventory.modes[0].registrations
    ]
    assert actual == expected


def test_removed_registration_breaks_fixed_mode_oracle():
    inventory = load_inventory((ROOT / "contracts/mounted-operations.json").read_bytes())
    changed = copy.deepcopy(inventory)
    changed.modes[0].registrations.pop()
    with pytest.raises(AssertionError):
        assert_all_modes(changed)
