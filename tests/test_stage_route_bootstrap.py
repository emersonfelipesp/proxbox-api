"""Regression tests: every first-runnable sync stage route bootstraps NetBox first.

The netbox-proxbox plugin runs stages in a fixed order whose first two entries are
``devices`` and ``storage``.  On a fresh install those two routes are therefore
routinely the very first NetBox writes of a stage-by-stage sync -- and they used to
run without ``ensure_netbox_sync_dependencies``. Missing bootstrap support objects
then caused the write chain to fail behind misleading downstream errors.

The expected route list below is **transcribed by hand** from the plugin's stage-path
map, not derived from the route declarations these tests audit.  A list read back out
of the same declarations would be satisfied by any declaration, including a wrong one.
"""

from __future__ import annotations

import asyncio

import pytest

from proxbox_api.dependencies import ensure_netbox_sync_dependencies
from proxbox_api.main import app
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


def test_safe_upsert_captures_a_support_object_warning() -> None:
    async def _exercise() -> BootstrapStatus:
        status = BootstrapStatus()

        async def _fail() -> object:
            raise RuntimeError("connection refused")

        await _safe_upsert(status, "tag:Proxbox", _fail)
        return status

    status = asyncio.run(_exercise())
    assert status.warnings == [{"object": "tag:Proxbox", "error": "connection refused"}]
