"""Helpers to normalize proxmox SDK responses in async code paths."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterable, AsyncIterator


async def resolve_async(value: object) -> object:
    """Resolve awaitables/async iterables recursively in async code paths."""
    if inspect.isawaitable(value):
        return await resolve_async(await value)

    if isinstance(value, (AsyncIterator, AsyncIterable)):
        return [item async for item in value]

    return value
