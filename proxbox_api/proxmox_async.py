"""Helpers to normalize proxmox SDK responses across sync and async backends."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterable, AsyncIterator

from proxbox_api.netbox_async_bridge import run_coroutine_blocking


async def resolve_async(value: object) -> object:
    """Resolve awaitables/async iterables recursively in async code paths."""
    if inspect.isawaitable(value):
        return await resolve_async(await value)

    if isinstance(value, (AsyncIterator, AsyncIterable)):
        return [item async for item in value]

    return value


def resolve_sync(value: object) -> object:
    """Resolve awaitables/async iterables recursively in sync code paths."""
    if inspect.isawaitable(value):
        return resolve_sync(run_coroutine_blocking(value))

    if isinstance(value, (AsyncIterator, AsyncIterable)):

        async def _collect() -> list[object]:
            return [item async for item in value]

        return resolve_sync(run_coroutine_blocking(_collect()))

    return value
