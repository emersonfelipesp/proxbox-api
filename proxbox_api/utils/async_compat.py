"""Async compatibility helpers shared across route and session modules."""

from __future__ import annotations

import inspect


async def maybe_await(value: object) -> object:
    """Await async SQLModel results while tolerating sync test sessions."""
    if inspect.isawaitable(value):
        return await value
    return value
