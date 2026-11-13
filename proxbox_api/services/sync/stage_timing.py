"""Timing summary for sync stages.

A stage that takes long gives an operator no way to tell where the time went.
``log_stage_timing`` writes one line when a stage ends: the wall time, and how
many NetBox requests were sent and how long NetBox took to answer them.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import get_request_timing_totals

P = ParamSpec("P")
R = TypeVar("R")


def log_stage_timing(
    stage: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Log the duration of an async sync stage, including failed and cancelled runs.

    The NetBox counters are process-wide. When two stages run at the same time
    the NetBox numbers include the requests of both, so the line labels them as
    requests made *during* the stage.
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            started = time.perf_counter()
            requests_before, seconds_before, slow_before = get_request_timing_totals()
            outcome = "failed"
            try:
                result = await func(*args, **kwargs)
                outcome = "completed"
                return result
            finally:
                requests_after, seconds_after, slow_after = get_request_timing_totals()
                logger.info(
                    "Sync stage %s %s in %.1fs; NetBox requests during stage: %d "
                    "(%.1fs waiting on NetBox, %d slow)",
                    stage,
                    outcome,
                    time.perf_counter() - started,
                    requests_after - requests_before,
                    seconds_after - seconds_before,
                    slow_after - slow_before,
                )

        return wrapper

    return decorator
