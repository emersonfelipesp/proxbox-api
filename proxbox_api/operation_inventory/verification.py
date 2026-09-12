"""Lightweight source validation before a coverage-readiness disposition."""

from pathlib import Path

from .provenance import dependencies, distribution_source, source, source_paths
from .schema import Inventory, InventoryError


def verify_sources(inventory: Inventory, root: Path) -> None:
    """An old artifact cannot become ready after its source or dependency changes."""
    paths = [*source_paths(root), root / "contracts/operation-inventory-inputs.json"]
    expected = [source(path, root) for path in paths]
    if inventory.provenance.sources != expected:
        raise InventoryError("Inventory source manifest is stale")
    if inventory.provenance.dependencies != dependencies(root):
        raise InventoryError("Inventory dependency manifest is stale")
    frameworks = [
        distribution_source(name, f"{name}/routing.py") for name in ("fastapi", "starlette")
    ]
    if inventory.provenance.framework_sources != frameworks:
        raise InventoryError("Inventory framework source is stale")
    for operation in inventory.operations.values():
        identity = operation.handler.source
        actual = (
            source(root / identity.path, root)
            if identity.owner == "repository"
            else distribution_source(identity.owner, identity.path)
        )
        if actual != identity:
            raise InventoryError("Inventory callable source is stale")
