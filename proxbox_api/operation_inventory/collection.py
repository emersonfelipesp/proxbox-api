"""Real application composition without lifespan, dependencies, or handlers."""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI

from .adapter import walk
from .generated import bind, identities, load_documents
from .inputs import Inputs, ModeInput, load_inputs
from .provenance import dependencies, provenance, source
from .schema import Inventory, InventoryError, Mode, Operation, Registration, digest

OPTIONAL = {
    "proxbox_api.pbs.admin": ("pbs", "/pbs"),
    "proxbox_api.pbs.routes": ("pbs", "/pbs"),
    "proxbox_api.ceph.routes": ("ceph", "/ceph"),
    "proxbox_api.ceph.v2_routes": ("ceph", "/ceph/v2"),
    "proxbox_api.pdm.admin": ("pdm", "/pdm"),
    "proxbox_api.pdm.routes": ("pdm", "/pdm"),
}


def optional_sequences(inputs: Inputs, root: Path) -> dict[str, list[Operation]]:
    """Import every declared optional router explicitly; factory skips are errors."""
    declared = {row.module: (row.feature, row.prefix) for row in inputs.optional_routers}
    if declared != OPTIONAL or len(inputs.optional_routers) != len(declared):
        raise InventoryError("Optional router manifest differs from reviewed modules")
    sequences = {}
    for name, (_, prefix) in declared.items():
        module = importlib.import_module(name)
        app = FastAPI()
        app.include_router(module.router, prefix=prefix)
        rows = [row for row in walk(app.routes, root) if row.handler.module == name]
        if not rows:
            raise InventoryError("Optional router is empty")
        sequences[name] = rows
    return sequences


def check_optional(
    rows: list[Operation], mode: ModeInput, expected: dict[str, list[Operation]]
) -> None:
    """Compare complete optional sequences, not only one representative endpoint."""
    for name, sequence in expected.items():
        actual = [row for row in rows if row.handler.module == name]
        wanted = sequence if OPTIONAL[name][0] in mode.sidecars else []
        if actual != wanted:
            raise InventoryError(f"Optional router sequence differs: {name}")


def check_core(rows: list[Operation], mode: ModeInput) -> None:
    """Verify core admission facts without assigning any operation effect."""
    expected = ["/", "/ws/virtual-machines", "/ws", "/ssh/sessions/{session_id}/ws"]
    actual = [row.path for row in rows if row.protocol == "websocket"]
    if actual != (expected if mode.core else []):
        raise InventoryError("Core WebSocket sequence differs from the reviewed mode")


@contextmanager
def selected_features(raw: str) -> Iterator[None]:
    """Select one feature variant and always restore the previous process value.

    Collection runs inside long-lived processes, including pytest workers, so a
    leaked ``PROXBOX_FEATURES`` would silently change every later import-time
    feature decision in the same process.
    """
    present = "PROXBOX_FEATURES" in os.environ
    previous = os.environ.get("PROXBOX_FEATURES", "")
    os.environ["PROXBOX_FEATURES"] = raw
    try:
        yield
    finally:
        if present:
            os.environ["PROXBOX_FEATURES"] = previous
        else:
            os.environ.pop("PROXBOX_FEATURES", None)


def _collect_mode(
    root: Path,
    mode: ModeInput,
    documents: dict,
    generated: dict,
    optional: dict[str, list[Operation]],
) -> list[Operation]:
    from proxbox_api.app.factory import create_app
    from proxbox_api.routes.proxmox.runtime_generated import register_generated_proxmox_routes

    with selected_features(mode.raw):
        app = create_app()
        state = register_generated_proxmox_routes(app, openapi_documents=documents)
        rows = [bind(row, generated) for row in walk(app.routes, root)]
        actual_names = [row.name for row in rows if row.generated]
        if actual_names != list(generated) or state["route_count"] != len(actual_names):
            raise InventoryError("Generated operation/version sequence is incomplete")
        check_optional(rows, mode, optional)
        check_core(rows, mode)
    return rows


def collect(root: Path) -> Inventory:
    """Collect every reviewed feature variant with explicitly generated routes."""
    inputs = load_inputs(root)
    dependencies(root)
    documents = load_documents(root, inputs.generated_versions)
    generated = identities(root, documents)
    optional = optional_sequences(inputs, root)
    definitions: dict[str, Operation] = {}
    modes = []
    previous = {}
    for mode in inputs.modes:
        rows = _collect_mode(root, mode, documents, generated, optional)
        if mode.equivalent and rows != previous.get(mode.equivalent):
            raise InventoryError("Feature normalization changed registration identity")
        previous[mode.name] = rows
        registrations = []
        for index, row in enumerate(rows):
            key = digest(row.model_dump())
            definitions[key] = row
            registrations.append(Registration(index=index, operation=key))
        modes.append(
            Mode(
                name=mode.name,
                feature_tokens=mode.raw,
                core=mode.core,
                sidecars=mode.sidecars,
                registrations=registrations,
            )
        )
        print(f"Collected {mode.name}: {len(rows)} registrations", flush=True)
    proof = provenance(root)
    manifest = source(root / "contracts/operation-inventory-inputs.json", root)
    proof = proof.model_copy(update={"sources": [*proof.sources, manifest]})
    return Inventory(schema_version=1, provenance=proof, operations=definitions, modes=modes)
