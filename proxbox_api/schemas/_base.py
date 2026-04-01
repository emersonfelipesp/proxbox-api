"""Shared Pydantic V2 base model configuration for proxbox-api schemas."""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import BaseModel, ConfigDict


class ProxboxBaseModel(BaseModel):
    """Base model with permissive extras and light mapping compatibility."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def __iter__(self) -> Iterator[tuple[str, object]]:
        return iter(self.model_dump(mode="python", by_alias=True, exclude_none=True).items())


class ProxboxLenientModel(ProxboxBaseModel):
    """Base model that allows arbitrary extras and keeps unknown keys."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )
