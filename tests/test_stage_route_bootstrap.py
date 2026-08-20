"""Regression tests: every first-runnable sync stage route bootstraps NetBox first.

The netbox-proxbox plugin runs stages in a fixed order whose first two entries are
``devices`` and ``storage``.  On a fresh install those two routes are therefore
routinely the very first NetBox writes of a stage-by-stage sync -- and they used to
run without ``ensure_netbox_sync_dependencies``, so NetBox rejected every write with
``Custom field 'proxmox_last_updated' does not exist for this object type`` and the
whole ``device_roles -> clusters -> device_types -> devices`` chain failed behind
misleading downstream errors.

The expected route list below is **transcribed by hand** from the plugin's stage-path
map, not derived from the route declarations these tests audit.  A list read back out
of the same declarations would be satisfied by any declaration, including a wrong one.
"""

from __future__ import annotations

import asyncio

import pytest

from proxbox_api.dependencies import ensure_netbox_sync_dependencies
from proxbox_api.main import app
from proxbox_api.services.custom_fields import (
    CUSTOM_FIELD_INVENTORY,
    _custom_field_failure_entry,
    _custom_field_failure_message,
    describe_custom_field_failure,
)
from proxbox_api.services.netbox_bootstrap import BootstrapStatus, _safe_upsert

# Transcribed from netbox_proxbox/sync_types.py::_SYNC_TYPE_PATH -- the stage paths a
# stage-by-stage sync can hit *before* any VM route has bootstrapped, plus their
# non-streaming siblings.  Keep in sync by hand when a stage is added.
FIRST_STAGE_WRITE_PATHS: frozenset[str] = frozenset(
    {
        "/dcim/devices/create",
        "/dcim/devices/create/stream",
        "/virtualization/virtual-machines/storage/create",
        "/virtualization/virtual-machines/storage/create/stream",
    }
)

# Routes that already bootstrapped before this change; asserted here so a refactor
# cannot quietly drop one while the new routes keep the suite green.
ESTABLISHED_BOOTSTRAP_PATHS: frozenset[str] = frozenset(
    {
        "/virtualization/virtual-machines/create",
        "/virtualization/virtual-machines/create/stream",
        "/full-update",
        "/full-update/stream",
    }
)


def _iter_effective_routes(routes: object) -> "list[object]":
    """Flatten an app's route table into objects exposing ``path``/``methods``/``dependant``.

    FastAPI materializes ``include_router()`` lazily in recent versions: ``app.routes``
    holds wrapper objects whose effective (prefixed) routes are produced on demand.
    Older versions put ``APIRoute`` objects there directly.  Handle both, so this guard
    keeps working across a FastAPI upgrade instead of silently matching nothing --
    a route table that resolves to zero entries would make every assertion below vacuous.
    """
    flattened: list[object] = []
    for route in routes:  # type: ignore[union-attr]
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            flattened.extend(_iter_effective_routes(candidates()))
            continue
        if hasattr(route, "dependant") and hasattr(route, "path"):
            flattened.append(route)
    return flattened


def _get_routes() -> dict[str, object]:
    resolved = {
        route.path: route  # type: ignore[attr-defined]
        for route in _iter_effective_routes(app.routes)
        if "GET" in (getattr(route, "methods", None) or set())
    }
    assert resolved, "no GET routes resolved from the application; the traversal is broken"
    return resolved


def _bootstrap_indexes(route: object) -> list[int]:
    """Positions of the bootstrap dependency within the route's solved dependency list."""
    return [
        index
        for index, dependency in enumerate(route.dependant.dependencies)
        if dependency.call is ensure_netbox_sync_dependencies
    ]


@pytest.mark.parametrize("path", sorted(FIRST_STAGE_WRITE_PATHS | ESTABLISHED_BOOTSTRAP_PATHS))
def test_stage_write_route_is_mounted(path: str) -> None:
    """The transcribed path must actually exist on the mounted app.

    Without this, a renamed or unmounted route would make the bootstrap assertion
    below vacuous instead of red -- a guard that cannot see the thing it guards.
    """
    assert path in _get_routes(), f"{path} is not mounted on the application"


@pytest.mark.parametrize("path", sorted(FIRST_STAGE_WRITE_PATHS | ESTABLISHED_BOOTSTRAP_PATHS))
def test_stage_write_route_bootstraps_netbox(path: str) -> None:
    route = _get_routes()[path]
    assert _bootstrap_indexes(route), (
        f"{path} does not bootstrap NetBox sync dependencies; a fresh install that "
        f"reaches this route first will have every write rejected"
    )


@pytest.mark.parametrize("path", sorted(FIRST_STAGE_WRITE_PATHS))
def test_bootstrap_is_ordered_before_route_data_dependencies(path: str) -> None:
    """The bootstrap must be solved before the route's own data-producing dependencies.

    FastAPI prepends route-level ``dependencies=[...]`` to the dependant's sub-dependency
    list, so declaring it there -- rather than as a function parameter -- is what makes
    the ordering real.  Asserting the index rather than mere presence is what keeps a
    future move to a plain parameter from silently reintroducing the defect.
    """
    route = _get_routes()[path]
    indexes = _bootstrap_indexes(route)
    assert indexes, f"{path} has no bootstrap dependency at all"

    # The property is "before the route's own parameter-backed dependencies", not
    # "at index zero". Asserting index zero would also fail if a future route-level
    # dependency (auth, tracing) were declared ahead of the bootstrap, which does not
    # reintroduce the defect. Parameter-backed dependencies are exactly the ones with
    # a name; route-level ones are parameterless.
    named_indexes = [
        index
        for index, dependency in enumerate(route.dependant.dependencies)  # type: ignore[attr-defined]
        if dependency.name is not None
    ]
    assert named_indexes, (
        f"{path} has no parameter-backed dependencies, so this ordering guard proves "
        f"nothing; the route or this expectation has changed"
    )
    assert indexes[0] < min(named_indexes), (
        f"{path} solves the parameter-backed dependency at index {min(named_indexes)} "
        f"before the NetBox bootstrap at index {indexes[0]}; the bootstrap must run "
        f"first so support objects exist before any write is attempted"
    )


def test_bootstrap_dependency_is_declared_at_route_level_not_as_parameter() -> None:
    """A route-level declaration is parameterless; a function parameter is not.

    This is the property that distinguishes the correct wiring from the wiring that
    merely happens to pass today.
    """
    routes = _get_routes()
    for path in sorted(FIRST_STAGE_WRITE_PATHS):
        route = routes[path]
        parameterless = [
            dependency
            for dependency in route.dependant.dependencies
            if dependency.call is ensure_netbox_sync_dependencies and dependency.name is None
        ]
        assert parameterless, (
            f"{path} resolves the NetBox bootstrap through a named parameter; route-level "
            f"`dependencies=[...]` is required for the ordering guarantee"
        )


# --------------------------------------------------------------------------------------
# Custom-field failure reporting
#
# Pre-creating a Proxbox custom field with the wrong type blocks the bootstrap
# permanently, because NetBox refuses type changes on existing custom fields. The raw
# NetBox message names neither the type Proxbox expects nor the remedy, so recovery
# previously required reading the source.
# --------------------------------------------------------------------------------------

# Transcribed from the reporter's NetBox response, verbatim.
NETBOX_TYPE_CHANGE_ERROR = '{"type":["Changing the type of custom fields is not supported."]}'


def _inventory_entry(name: str) -> dict[str, object]:
    for field in CUSTOM_FIELD_INVENTORY:
        if field.get("name") == name:
            return dict(field)
    raise AssertionError(f"{name} is not in CUSTOM_FIELD_INVENTORY")


def test_custom_field_inventory_still_declares_the_field_from_the_report() -> None:
    """Anchor the case the report describes, so the tests below cannot go vacuous."""
    field = _inventory_entry("proxmox_last_updated")
    assert field["type"] == "datetime"


def test_type_change_failure_names_the_expected_type_and_the_remedy() -> None:
    entry = _custom_field_failure_entry(
        _inventory_entry("proxmox_last_updated"), NETBOX_TYPE_CHANGE_ERROR
    )
    assert entry["name"] == "proxmox_last_updated"
    assert entry["expected_type"] == "datetime"
    # The whole point: an operator must be able to act on this without reading source.
    assert "datetime" in entry["remedy"]
    assert "delete" in entry["remedy"].lower()
    assert "virtualization.virtualmachine" in entry["expected_object_types"]
    # Losing stored values is a consequence the operator must be warned about before
    # being told to delete the field.
    assert "discards the values" in entry["remedy"]


def test_non_type_change_failure_still_names_the_expected_definition() -> None:
    """A permissions or transport failure gets a different remedy, not the delete advice."""
    entry = _custom_field_failure_entry(
        _inventory_entry("proxmox_vm_id"), "403 Forbidden: insufficient permission"
    )
    assert entry["expected_type"] == "integer"
    assert "permission" in entry["remedy"]
    assert "delete" not in entry["remedy"].lower()


def test_failure_entry_survives_an_inventory_row_missing_its_fields() -> None:
    """A malformed inventory row must degrade, not raise, mid-bootstrap."""
    entry = _custom_field_failure_entry({}, "boom")
    assert entry["name"] == "unknown"
    assert entry["expected_type"] == "unknown"
    assert entry["remedy"]


def test_failure_message_says_each_distinct_remedy_once() -> None:
    """A whole-inventory failure must not concatenate one remedy per field.

    The message is copied into an SSE frame, an HTTP error body and a long-lived
    NetBox job log, so repeating an identical remedy 25 times is a real cost.
    """
    entries = [
        _custom_field_failure_entry(dict(field), "403 Forbidden: insufficient permission")
        for field in CUSTOM_FIELD_INVENTORY
    ]
    assert len(entries) > 3, "inventory too small for this test to mean anything"

    message = _custom_field_failure_message(entries)
    # Every failed field is still named.
    for entry in entries:
        assert entry["name"] in message
    # But the shared remedy text appears a bounded number of times.
    distinct = {entry["remedy"] for entry in entries}
    for remedy in distinct:
        assert message.count(remedy) <= 1


def test_failure_message_reports_withheld_remedies_instead_of_truncating_silently() -> None:
    """A guard that drops findings quietly reads as 'nothing else was wrong'."""
    entries = [
        _custom_field_failure_entry(dict(field), f"distinct failure {index}")
        for index, field in enumerate(CUSTOM_FIELD_INVENTORY[:6])
    ]
    distinct = {entry["remedy"] for entry in entries}
    assert len(distinct) > 3, "fixture must produce more distinct remedies than the cap"

    message = _custom_field_failure_message(entries)
    assert "further distinct remedy" in message
    assert "detail.failed_fields" in message


def test_failure_message_omits_the_withheld_note_when_nothing_is_withheld() -> None:
    entries = [
        _custom_field_failure_entry(dict(CUSTOM_FIELD_INVENTORY[0]), "boom"),
    ]
    message = _custom_field_failure_message(entries)
    assert "further distinct remedy" not in message
    assert entries[0]["remedy"] in message


# --------------------------------------------------------------------------------------
# Bootstrap-warning enrichment
#
# The log line an operator actually reads when a wrong-typed custom field blocks the
# bootstrap comes from run_netbox_bootstrap()'s per-entry warning capture, not from the
# extras route.  Enriching only the route would put the remedy on the path nobody reads.
# --------------------------------------------------------------------------------------


def test_bootstrap_warning_for_a_custom_field_carries_the_remedy() -> None:
    described = describe_custom_field_failure(
        "custom_field:proxmox_last_updated", NETBOX_TYPE_CHANGE_ERROR
    )
    assert described is not None
    assert described["expected_type"] == "datetime"
    assert "delete" in described["remedy"].lower()


def test_bootstrap_warning_for_a_non_custom_field_entry_is_left_alone() -> None:
    """Tags, cluster types and device roles have no expected custom-field type."""
    for label in ("tag:Proxbox", "manufacturer:proxmox", "device_role:proxmox-node"):
        assert describe_custom_field_failure(label, NETBOX_TYPE_CHANGE_ERROR) is None


def test_bootstrap_warning_for_an_unknown_custom_field_is_left_alone() -> None:
    """A label naming a field that is not in the inventory must not be invented."""
    assert describe_custom_field_failure("custom_field:not_a_proxbox_field", "boom") is None


def test_safe_upsert_attaches_the_remedy_to_the_captured_warning() -> None:
    """Drive the real capture path, not just the helper it calls.

    Asserting only on ``describe_custom_field_failure`` would stay green if
    ``_safe_upsert`` stopped calling it -- a guard that cannot see the thing it guards.
    """

    async def _exercise() -> BootstrapStatus:
        status = BootstrapStatus()

        async def _fail() -> object:
            raise RuntimeError(NETBOX_TYPE_CHANGE_ERROR)

        await _safe_upsert(status, "custom_field:proxmox_last_updated", _fail)
        return status

    status = asyncio.run(_exercise())
    assert len(status.warnings) == 1
    warning = status.warnings[0]
    assert warning["object"] == "custom_field:proxmox_last_updated"
    assert NETBOX_TYPE_CHANGE_ERROR in warning["error"]
    assert warning["expected_type"] == "datetime"
    assert "delete" in warning["remedy"].lower()


def test_safe_upsert_leaves_a_non_custom_field_warning_unenriched() -> None:
    async def _exercise() -> BootstrapStatus:
        status = BootstrapStatus()

        async def _fail() -> object:
            raise RuntimeError("connection refused")

        await _safe_upsert(status, "tag:Proxbox", _fail)
        return status

    status = asyncio.run(_exercise())
    assert status.warnings == [{"object": "tag:Proxbox", "error": "connection refused"}]


def test_stored_error_is_capped_but_detection_still_sees_the_full_text() -> None:
    """A huge NetBox response must not bloat the SSE frame -- without losing the verdict.

    Detection has to run against the *full* text; capping first would classify a
    type-change refusal buried past the cap as a generic permission problem.
    """
    padding = "x" * 5000
    error = f"{padding} {NETBOX_TYPE_CHANGE_ERROR}"
    entry = _custom_field_failure_entry(_inventory_entry("proxmox_last_updated"), error)

    assert len(entry["error"]) < len(error)
    assert "more characters" in entry["error"], "a clipped error must say it was clipped"
    # The marker sits past the cap, so this only passes if detection ran on the full text.
    assert "delete" in entry["remedy"].lower()


def test_short_error_is_stored_verbatim() -> None:
    entry = _custom_field_failure_entry(_inventory_entry("proxmox_vm_id"), "boom")
    assert entry["error"] == "boom"
