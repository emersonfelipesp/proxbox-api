"""Strict, bounded contracts for registration facts and unresolved coverage."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BYTES = 96 * 1024 * 1024
MAX_NODES = 3_000_000
Text = Annotated[str, Field(min_length=1, max_length=2048)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Natural = Annotated[int, Field(ge=0, le=1_000_000)]
Positive = Annotated[int, Field(ge=1, le=1_000_000)]


class InventoryError(ValueError):
    """An inventory input cannot establish complete registration evidence."""


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise InventoryError("Duplicate document key")
        result[key] = value
    return result


def _children(value: object) -> list[object]:
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise InventoryError("JSON object keys must be strings")
        return [*value.keys(), *value.values()]
    if type(value) is list:
        return cast(list[object], value)
    if type(value) is float and not math.isfinite(value):
        raise InventoryError("Non-finite JSON number")
    if type(value) not in (str, int, float, bool, type(None)):
        raise InventoryError("Unsupported JSON value type")
    return []


def check_shape(value: object) -> None:
    """Reject coercion, oversized depth, invalid text, and excessive node counts."""
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 48 or nodes > MAX_NODES:
            raise InventoryError("Document exceeds structural bounds")
        if type(item) is str:
            item.encode("utf-8", errors="strict")
            if "\x00" in item:
                raise InventoryError("NUL is forbidden")
        pending.extend((child, depth + 1) for child in _children(item))


def canonical(value: object) -> bytes:
    """Serialize only bounded canonical JSON-compatible builtin values."""
    check_shape(value)
    result = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    encoded = (result + "\n").encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise InventoryError("Document exceeds byte bound")
    return encoded


def digest(value: object) -> str:
    """Hash a validated, canonical value including its terminal newline."""
    return hashlib.sha256(canonical(value)).hexdigest()


def parse(raw: bytes) -> object:
    """Read strict bounded JSON without duplicate-key loss."""
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise InventoryError("Invalid document bytes")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        check_shape(value)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as error:
        raise InventoryError("Invalid inventory JSON") from error
    return value


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Source(StrictRecord):
    owner: Text
    path: Text
    sha256: Digest

    @model_validator(mode="after")
    def relative_path(self) -> Self:
        if self.path.startswith("/") or "\\" in self.path:
            raise ValueError("Source path must be repository/distribution relative")
        if any(part in ("", ".", "..") for part in self.path.split("/")):
            raise ValueError("Noncanonical source path")
        return self


class CallableIdentity(StrictRecord):
    module: Text
    qualname: Text
    line: Positive
    source: Source


class Mount(StrictRecord):
    path: Text
    name: str | None


class Generated(StrictRecord):
    version: Text
    schema_version: Text
    alias: bool
    upstream_path: Text
    upstream_method: Literal["GET", "POST", "PUT", "DELETE"]
    operation_id: Text
    operation_sha256: Digest
    schema_source: Source

    @model_validator(mode="after")
    def alias_identity(self) -> Self:
        if self.alias and self.version != "latest":
            raise ValueError("Only latest has an unversioned alias")
        if not self.upstream_path.startswith("/"):
            raise ValueError("Upstream path must be an absolute template")
        return self


class Operation(StrictRecord):
    protocol: Literal["http", "websocket", "mount"]
    path: Text
    declared_path: Text
    methods: list[Text]
    route_kind: Text
    name: Text
    mounts: list[Mount]
    handler: CallableIdentity
    generated: Generated | None

    @model_validator(mode="after")
    def consistent_identity(self) -> Self:
        kinds = {
            "APIRoute": "http",
            "Route": "http",
            "APIWebSocketRoute": "websocket",
            "WebSocketRoute": "websocket",
            "Mount": "mount",
        }
        if kinds.get(self.route_kind) != self.protocol:
            raise ValueError("Route kind and protocol disagree")
        if not self.path.startswith("/") or not self.declared_path.startswith("/"):
            raise ValueError("Route paths must be absolute path templates")
        if self.methods != sorted(set(self.methods)):
            raise ValueError("Methods must be unique and sorted")
        allowed = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
        if any(method not in allowed for method in self.methods):
            raise ValueError("Unsupported HTTP method")
        if bool(self.methods) != (self.protocol == "http"):
            raise ValueError("Protocol and methods disagree")
        if self.generated and self.methods != [self.generated.upstream_method]:
            raise ValueError("Generated method identity disagrees")
        return self


class Registration(StrictRecord):
    index: Natural
    operation: Digest


class Mode(StrictRecord):
    name: Text
    feature_tokens: str
    core: bool
    sidecars: list[Literal["pbs", "ceph", "pdm"]]
    registrations: list[Registration]


class Dependency(StrictRecord):
    name: Text
    version: Text


class Provenance(StrictRecord):
    python: Text
    dependencies: list[Dependency]
    sources: list[Source]
    framework_sources: list[Source]
    imported_modules: dict[Text, Source]


class Inventory(StrictRecord):
    schema_version: Literal[1]
    provenance: Provenance
    operations: dict[Digest, Operation]
    modes: list[Mode]

    @model_validator(mode="after")
    def complete_references(self) -> Self:
        if not self.operations or not self.modes:
            raise ValueError("Empty inventory")
        names = [mode.name for mode in self.modes]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate mode")
        referenced: set[str] = set()
        for mode in self.modes:
            if [row.index for row in mode.registrations] != list(range(len(mode.registrations))):
                raise ValueError("Registration order is incomplete")
            referenced.update(row.operation for row in mode.registrations)
        if referenced != self.operations.keys():
            raise ValueError("Missing or orphan operation")
        for key, operation in self.operations.items():
            if digest(operation.model_dump()) != key:
                raise ValueError("Operation identity digest differs")
        return self


def load_inventory(raw: bytes) -> Inventory:
    """Validate raw field types before Pydantic literal aliases can coerce them."""
    value = parse(raw)
    if (
        type(value) is not dict
        or type(cast(dict[str, object], value).get("schema_version")) is not int
    ):
        raise InventoryError("Invalid inventory schema version")
    return Inventory.model_validate(value)


Effect = Literal[
    "local_persistence",
    "netbox_inventory_mutation",
    "material_reveal",
    "managed_read",
    "managed_mutation",
    "interactive_capability_creation",
    "interactive_capability_consumption",
    "no_external_effect",
]


class Evidence(StrictRecord):
    status: Literal["unresolved", "evidenced", "not_applicable"]
    rationale: Text
    citations: list[Text]

    @model_validator(mode="after")
    def evidence_required(self) -> Self:
        if self.status != "unresolved" and not self.citations:
            raise ValueError("Resolved dispositions require explicit evidence")
        return self


class Coverage(StrictRecord):
    operation: Digest
    effects: list[Effect]
    effect_evidence: Evidence
    callers: Evidence
    final_handler: Evidence
    target: Evidence
    procedure_version: Evidence
    transport: Evidence
    input_schema: Evidence
    result_schema: Evidence
    credential_purpose_fields: Evidence
    endpoint_transport_gates: Evidence
    permissions_approval: Evidence
    timeout: Evidence
    idempotency_reconciliation: Evidence
    terminal_task_proof: Evidence
    audit_links: Evidence
    tests: Evidence

    @model_validator(mode="after")
    def effect_consistency(self) -> Self:
        if len(self.effects) != len(set(self.effects)):
            raise ValueError("Duplicate effect")
        if "no_external_effect" in self.effects and len(self.effects) != 1:
            raise ValueError("Contradictory effects")
        if self.effect_evidence.status != "unresolved" and not self.effects:
            raise ValueError("Effects remain unclassified")
        return self


class CoverageDocument(StrictRecord):
    schema_version: Literal[1]
    inventory_sha256: Digest
    records: list[Coverage]


def load_coverage(raw: bytes) -> CoverageDocument:
    """Parse a strict coverage document; duplicate keys and numeric aliases fail."""
    value = parse(raw)
    if (
        type(value) is not dict
        or type(cast(dict[str, object], value).get("schema_version")) is not int
    ):
        raise InventoryError("Invalid coverage schema version")
    return CoverageDocument.model_validate(value)
