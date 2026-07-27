"""Helpers for mandatory async work that must outlive caller cancellation."""

from __future__ import annotations

import asyncio
from typing import TypeVar

_TaskResultT = TypeVar("_TaskResultT")


async def await_task_through_repeated_cancellation(
    task: asyncio.Task[_TaskResultT],
) -> _TaskResultT:
    """Finish ``task`` despite repeated caller cancellation, then re-raise.

    A single ``asyncio.shield`` protects the inner task but does not make the
    outer wait cancellation-resistant. Keep shielding the same already-started
    task until it reaches a terminal state, remember caller cancellation, read
    the result without another suspension point, and only then propagate
    ``CancelledError``.
    """

    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done() and task.cancelled():
                raise
            cancellation_requested = True

    result = task.result()
    if cancellation_requested:
        raise asyncio.CancelledError
    return result
