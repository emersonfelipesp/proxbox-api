"""Synchronous proxy wrappers for netbox-sdk async facade objects."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterable, AsyncIterator

from proxbox_api.netbox_async_bridge import run_coroutine_blocking


def _collect_async_iter(it: AsyncIterator[object]) -> list[object]:
    async def _collect() -> list[object]:
        result = []
        async for item in it:
            result.append(item)
        return result

    return run_coroutine_blocking(_collect())


def _wrap(value: object) -> object:
    if inspect.iscoroutine(value):
        return _wrap(run_coroutine_blocking(value))
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_wrap(item) for item in value)
    if isinstance(value, dict):
        return value
    if isinstance(value, (AsyncIterator, AsyncIterable)):
        return _wrap(_collect_async_iter(value))
    if hasattr(value, "serialize") or hasattr(value, "__dict__"):
        return SyncProxy(value)
    return value


class SyncProxy:
    """Proxy that exposes async facade objects with sync call behavior."""

    def __init__(self, obj: object) -> None:
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name: str) -> object:
        attr = getattr(object.__getattribute__(self, "_obj"), name)
        if callable(attr):

            def _call(*args: object, **kwargs: object) -> object:
                return _wrap(attr(*args, **kwargs))

            return _call
        return _wrap(attr)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(object.__getattribute__(self, "_obj"), name, value)

    def __iter__(self):
        obj = object.__getattribute__(self, "_obj")
        if hasattr(obj, "__iter__"):
            return iter(obj)
        raise TypeError(f"{type(obj).__name__} is not iterable")

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "_obj"))

    def dict(self) -> dict[str, object]:
        obj = object.__getattribute__(self, "_obj")
        if hasattr(obj, "serialize"):
            return obj.serialize()
        if hasattr(obj, "dict"):
            return obj.dict()
        return {}

    @property
    def json(self) -> dict[str, object]:
        return self.dict()

    def get(self, key: str, default: object = None) -> object:
        return self.dict().get(key, default)
