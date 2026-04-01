"""In-memory cache helper used across API workflows."""

from __future__ import annotations

from typing import Any

_MISSING = object()


class Cache:
    def __init__(self) -> None:
        self.cache: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.cache.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.cache

    def set(self, key: str, value: Any) -> None:
        self.cache[key] = value

    def delete(self, key: str) -> bool:
        result = self.cache.pop(key, _MISSING)
        return result is not _MISSING

    def return_cache(self) -> dict[str, Any]:
        return dict(self.cache)

    def clear_cache(self) -> None:
        self.cache.clear()


global_cache = Cache()
