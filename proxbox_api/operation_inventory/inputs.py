"""Reviewed extractor inputs, separate from application runtime configuration."""

from pathlib import Path
from typing import Literal, Self, cast

from pydantic import model_validator

from .provenance import regular_file
from .schema import InventoryError, StrictRecord, Text, parse


class OptionalRouter(StrictRecord):
    feature: Literal["pbs", "ceph", "pdm"]
    module: Text
    prefix: Text


class ModeInput(StrictRecord):
    name: Text
    raw: str
    core: bool
    sidecars: list[Literal["pbs", "ceph", "pdm"]]
    equivalent: str | None


class Inputs(StrictRecord):
    schema_version: Literal[1]
    generated_versions: list[Text]
    optional_routers: list[OptionalRouter]
    modes: list[ModeInput]

    @model_validator(mode="after")
    def complete_modes(self) -> Self:
        names = [mode.name for mode in self.modes]
        states = {(mode.core, tuple(mode.sidecars)) for mode in self.modes}
        if len(names) != 22 or len(set(names)) != 22 or len(states) != 15:
            raise ValueError("The complete mode matrix is required")
        if len(self.optional_routers) != 6 or not self.generated_versions:
            raise ValueError("Incomplete optional/generated input manifest")
        return self


def load_inputs(root: Path) -> Inputs:
    """Require the committed manifest; never substitute default discovery inputs."""
    value = parse(regular_file(root / "contracts/operation-inventory-inputs.json").read_bytes())
    if (
        type(value) is not dict
        or type(cast(dict[str, object], value).get("schema_version")) is not int
    ):
        raise InventoryError("Invalid input manifest version")
    return Inputs.model_validate(value)
