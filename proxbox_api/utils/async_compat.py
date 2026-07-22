"""Async compatibility helpers shared across route and session modules."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import TypeVar, cast, overload

T = TypeVar("T")


@overload
async def maybe_await(value: Awaitable[T]) -> T: ...


@overload
async def maybe_await(value: T) -> T: ...


async def maybe_await(value: T | Awaitable[T]) -> T:
    """Await async SQLModel results while tolerating sync test sessions."""
    if inspect.isawaitable(value):
        return await cast("Awaitable[T]", value)
    return value
