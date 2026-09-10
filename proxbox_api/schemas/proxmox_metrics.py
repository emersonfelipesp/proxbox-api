"""Typed contracts for bounded Proxmox cluster metric pulls."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from proxbox_api.schemas._base import ProxboxStrictModel

MAX_PULL_FILTERS = 128
MAX_PULL_ROWS = 5000
MAX_PULL_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PULL_TIMESTAMP = 4_102_444_800

_METRIC_OBJECT_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*$")
_METRIC_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")


def _validate_filter_values(
    values: list[str], *, pattern: re.Pattern[str], label: str
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = value.strip()
        segments = candidate.split("/")
        if (
            not candidate
            or not pattern.fullmatch(candidate)
            or any(segment in {".", ".."} for segment in segments)
        ):
            raise ValueError(f"{label} contains unsupported characters")
        if candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)
    return normalized


class ProxmoxMetricsPullRequest(ProxboxStrictModel):
    """Bounded input for the fixed Proxmox cluster metrics export operation."""

    source: Literal["database", "netbox"] = "database"
    endpoint_id: Annotated[int | None, Field(ge=1)] = None
    target_name: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    target_domain: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    target_ip_address: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    start_time: Annotated[int | None, Field(ge=0, le=MAX_PULL_TIMESTAMP)] = None
    history: bool = False
    local_only: bool = False
    object_ids: Annotated[list[str], Field(max_length=MAX_PULL_FILTERS)] = Field(
        default_factory=list
    )
    object_prefixes: Annotated[list[str], Field(max_length=MAX_PULL_FILTERS)] = Field(
        default_factory=list
    )
    metric_names: Annotated[list[str], Field(max_length=MAX_PULL_FILTERS)] = Field(
        default_factory=list
    )
    max_rows: Annotated[int, Field(ge=1, le=MAX_PULL_ROWS)] = 1000
    max_response_bytes: Annotated[int, Field(ge=1024, le=MAX_PULL_RESPONSE_BYTES)] = 1024 * 1024

    @field_validator("target_name", "target_domain", "target_ip_address")
    @classmethod
    def validate_target_selector(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
            raise ValueError("target selector contains a control character")
        return candidate

    @field_validator("object_ids")
    @classmethod
    def validate_object_ids(cls, values: list[str]) -> list[str]:
        return _validate_filter_values(values, pattern=_METRIC_OBJECT_PATTERN, label="object_ids")

    @field_validator("object_prefixes")
    @classmethod
    def validate_object_prefixes(cls, values: list[str]) -> list[str]:
        return _validate_filter_values(
            values, pattern=_METRIC_OBJECT_PATTERN, label="object_prefixes"
        )

    @field_validator("metric_names")
    @classmethod
    def validate_metric_names(cls, values: list[str]) -> list[str]:
        return _validate_filter_values(values, pattern=_METRIC_NAME_PATTERN, label="metric_names")

    @model_validator(mode="after")
    def validate_history_window(self) -> ProxmoxMetricsPullRequest:
        if self.history and self.start_time is None:
            raise ValueError("start_time is required when history is enabled")
        selectors = (
            self.target_name,
            self.target_domain,
            self.target_ip_address,
        )
        if self.endpoint_id is not None and any(selector is not None for selector in selectors):
            raise ValueError("endpoint_id cannot be combined with another target selector")
        if sum(selector is not None for selector in selectors) > 1:
            raise ValueError("provide at most one target selector")
        return self


class ProxmoxMetricSample(ProxboxStrictModel):
    """One normalized sample returned by Proxmox cluster metrics export."""

    object_id: str
    metric: str
    timestamp: int
    value: float
    metric_type: Literal["gauge", "counter", "derive"] | None = None
    source: Literal["pull"] = "pull"


class ProxmoxMetricsPullWindow(ProxboxStrictModel):
    """The effective time window represented by a pull response."""

    start: str
    stop: str


class ProxmoxMetricsPullResponse(ProxboxStrictModel):
    """Stable response family shared with the plugin metrics normalizer."""

    columns: list[str]
    rows: list[dict[str, object]]
    row_count: int
    truncated: bool
    query_window: ProxmoxMetricsPullWindow
    captured_at: datetime
    response_format: Literal["proxmox_export"] = "proxmox_export"
    deduplicated_count: int = 0
