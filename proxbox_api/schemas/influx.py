"""Typed, bounded request and response contracts for InfluxDB v2 queries."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator

from proxbox_api.schemas._base import ProxboxStrictModel

InfluxFilterScope = Literal["tag", "field"]
InfluxFilterOperator = Literal["==", "!="]
InfluxAggregationFunction = Literal["count", "first", "last", "max", "mean", "min", "sum"]
InfluxResponseFormat = Literal["annotated_csv", "json"]

MAX_INFLUX_URL_LENGTH = 2048
MAX_INFLUX_FILTERS = 32
MAX_INFLUX_FIELDS = 16
MAX_INFLUX_ROWS = 5000
MAX_INFLUX_RESPONSE_BYTES = 8 * 1024 * 1024

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:/-]{0,63}$")
_DURATION_PATTERN = re.compile(r"^-?(?:\d+(?:ns|us|µs|ms|s|m|h|d|w|mo|y))+$")
_POSITIVE_DURATION_PATTERN = re.compile(
    r"^\d+(?:ns|us|µs|ms|s|m|h|d|w|mo|y)(?:\d+(?:ns|us|µs|ms|s|m|h|d|w|mo|y))*$"
)


def _validate_text(value: str, *, name: str, max_length: int) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > max_length:
        raise ValueError(f"{name} is too long")
    if _CONTROL_CHARACTERS.search(value):
        raise ValueError(f"{name} contains a control character")
    return value


def validate_influx_time(value: str | None, *, name: str, allow_none: bool = False) -> str | None:
    """Validate an Influx relative duration or an RFC3339 timestamp."""
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = _validate_text(value, name=name, max_length=64)
    if _DURATION_PATTERN.fullmatch(value):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an Influx duration or RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} timestamp must include a timezone")
    return value


def validate_influx_duration(value: str, *, name: str) -> str:
    value = _validate_text(value, name=name, max_length=32)
    if not _POSITIVE_DURATION_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be a positive Influx duration")
    return value


class InfluxFilter(ProxboxStrictModel):
    """One safe equality or inequality predicate for a tag or field."""

    key: Annotated[str, Field(min_length=1, max_length=64)]
    value: Annotated[str, Field(max_length=256)]
    scope: InfluxFilterScope = "tag"
    operator: InfluxFilterOperator = "=="

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        value = _validate_text(value, name="filter key", max_length=64)
        if not _KEY_PATTERN.fullmatch(value):
            raise ValueError("filter key contains unsupported characters")
        return value

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        return _validate_text(value, name="filter value", max_length=256)


class InfluxAggregation(ProxboxStrictModel):
    """Optional bounded aggregateWindow operation."""

    every: Annotated[str, Field(min_length=1, max_length=32)]
    function: InfluxAggregationFunction

    @field_validator("every")
    @classmethod
    def validate_every(cls, value: str) -> str:
        return validate_influx_duration(value, name="aggregation interval")


class InfluxQueryRequest(ProxboxStrictModel):
    """Structured input for one InfluxDB v2 query.

    The model intentionally has no raw Flux field. Every query expression is
    assembled by the provider-neutral client from these bounded values.
    """

    url: Annotated[AnyHttpUrl, Field(max_length=MAX_INFLUX_URL_LENGTH)]
    org: Annotated[str, Field(min_length=1, max_length=128)]
    bucket: Annotated[str, Field(min_length=1, max_length=128)]
    token: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    verify_ssl: bool = True
    timeout_seconds: Annotated[float, Field(ge=1, le=30)] = 10.0
    start: Annotated[str, Field(min_length=2, max_length=64)] = "-1h"
    stop: Annotated[str | None, Field(max_length=64)] = None
    measurement: Annotated[str, Field(min_length=1, max_length=128)]
    field: Annotated[str | None, Field(max_length=128)] = None
    fields: Annotated[list[str], Field(max_length=MAX_INFLUX_FIELDS)] = Field(default_factory=list)
    filters: Annotated[list[InfluxFilter], Field(max_length=MAX_INFLUX_FILTERS)] = Field(
        default_factory=list
    )
    aggregation: InfluxAggregation | None = None
    max_rows: Annotated[int, Field(ge=1, le=MAX_INFLUX_ROWS)] = 1000
    max_response_bytes: Annotated[int, Field(ge=1024, le=MAX_INFLUX_RESPONSE_BYTES)] = 1024 * 1024

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("url must use https")
        if value.username is not None or value.password is not None:
            raise ValueError("url must not contain credentials")
        if value.query or value.fragment:
            raise ValueError("url must not contain a query or fragment")
        if len(str(value)) > MAX_INFLUX_URL_LENGTH:
            raise ValueError("url is too long")
        return value

    @field_validator("org", "bucket", "measurement", "field")
    @classmethod
    def validate_named_values(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "value")
        return _validate_text(value, name=str(field_name), max_length=128)

    @field_validator("start")
    @classmethod
    def validate_start(cls, value: str) -> str:
        return validate_influx_time(value, name="start") or "-1h"

    @field_validator("stop")
    @classmethod
    def validate_stop(cls, value: str | None) -> str | None:
        return validate_influx_time(value, name="stop", allow_none=True)

    @model_validator(mode="after")
    def validate_time_window(self) -> InfluxQueryRequest:
        if self.stop and self.stop == self.start:
            raise ValueError("stop must differ from start")
        if self.field is not None and self.fields:
            raise ValueError("field and fields cannot be combined")
        return self

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, values: list[str]) -> list[str]:
        return list(
            dict.fromkeys(_validate_text(value, name="field", max_length=128) for value in values)
        )


class InfluxQueryWindow(ProxboxStrictModel):
    """The bounded time window used to build the query."""

    start: str
    stop: str | None = None


class InfluxQueryResponse(ProxboxStrictModel):
    """Stable normalized result independent of Influx response encoding."""

    columns: list[str]
    rows: list[dict[str, object]]
    row_count: int
    truncated: bool
    query_window: InfluxQueryWindow
    captured_at: datetime
    response_format: InfluxResponseFormat
