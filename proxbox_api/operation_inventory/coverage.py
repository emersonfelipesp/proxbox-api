"""Disposition completeness is separate from generation and authorization."""

from .schema import Coverage, CoverageDocument, Evidence, Inventory, InventoryError, digest


def unresolved(inventory: Inventory) -> CoverageDocument:
    """Generate explicit unknowns, never a method-inferred safe disposition."""
    unknown = Evidence(
        status="unresolved", rationale="Effect and caller tracing is incomplete.", citations=[]
    )
    fields = {
        name: unknown for name in Coverage.model_fields if name not in {"operation", "effects"}
    }
    return CoverageDocument(
        schema_version=1,
        inventory_sha256=digest(inventory.model_dump()),
        records=[
            Coverage(operation=key, effects=[], **fields) for key in sorted(inventory.operations)
        ],
    )


def check_coverage(inventory: Inventory, coverage: CoverageDocument) -> list[str]:
    """Require an exact join and return every unresolved operation/column."""
    if coverage.inventory_sha256 != digest(inventory.model_dump()):
        raise InventoryError("Coverage binds a different inventory")
    identifiers = [row.operation for row in coverage.records]
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != inventory.operations.keys():
        raise InventoryError("Coverage has duplicate, missing, or foreign operations")
    missing = []
    for row in coverage.records:
        for name in Coverage.model_fields:
            value = getattr(row, name)
            if isinstance(value, Evidence) and value.status == "unresolved":
                missing.append(f"{row.operation}:{name}")
    return missing
