"""Synchronous proxy wrappers for netbox-sdk async facade objects."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncIterator
from typing import Any


def _run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {"value": None, "error": None}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if result["error"]:
        raise result["error"]
    return result["value"]


def _collect_async_iter(it: AsyncIterator[Any]) -> list[Any]:
    async def _collect() -> list[Any]:
        result = []
        async for item in it:
            result.append(item)
        return result

    return _run(_collect())


def _wrap(value: Any) -> Any:
    if inspect.iscoroutine(value):
        return _wrap(_run(value))
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_wrap(item) for item in value)
    if isinstance(value, dict):
        return value
    if hasattr(value, "__aiter__"):
        return _wrap(_collect_async_iter(value))
    if hasattr(value, "serialize") or hasattr(value, "__dict__"):
        return SyncProxy(value)
    return value


class SyncProxy:
    """Proxy that exposes async facade objects with sync call behavior."""

    def __init__(self, obj: Any) -> None:
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_obj"), name)
        if callable(attr):

            def _call(*args: Any, **kwargs: Any) -> Any:
                return _wrap(attr(*args, **kwargs))

            return _call
        return _wrap(attr)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_obj"), name, value)

    def __iter__(self):
        obj = object.__getattribute__(self, "_obj")
        if hasattr(obj, "__iter__"):
            return iter(obj)
        raise TypeError(f"{type(obj).__name__} is not iterable")

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "_obj"))

    def dict(self) -> dict[str, Any]:
        obj = object.__getattribute__(self, "_obj")
        if hasattr(obj, "serialize"):
            return obj.serialize()
        if hasattr(obj, "dict"):
            return obj.dict()
        return {}

    @property
    def json(self) -> dict[str, Any]:
        return self.dict()

    def get(self, key: str, default: Any = None) -> Any:
        return self.dict().get(key, default)
