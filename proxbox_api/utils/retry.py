"""Retry utilities for handling transient connection failures."""

from __future__ import annotations

import asyncio
import os
from typing import Callable, TypeVar

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger

T = TypeVar("T")

DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0


def _resolve_max_retries() -> int:
    raw = os.environ.get("PROXBOX_NETBOX_MAX_RETRIES", "").strip()
    if not raw:
        return DEFAULT_MAX_RETRIES
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_MAX_RETRIES
    return max(0, val)


def _resolve_base_delay() -> float:
    raw = os.environ.get("PROXBOX_NETBOX_RETRY_DELAY", "").strip()
    if not raw:
        return DEFAULT_BASE_DELAY
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_BASE_DELAY
    return max(0.0, val)


def _is_transient_netbox_error(error: Exception) -> bool:
    """Check if an error is transient and worth retrying."""
    error_str = str(error).lower()
    transient_indicators = [
        "connection refused",
        "cannot connect",
        "connect call failed",
        "timeout",
        "temporarily unavailable",
        "name or service not known",
        "no route to host",
        "network is unreachable",
        "connection slots are reserved",
        "remaining connection slots",
        "too many connections",
        "database unavailable",
        "psycopg2.errors",
        "operationalerror",
    ]
    return any(indicator in error_str for indicator in transient_indicators)


async def retry_async(
    coro: Callable[..., object],
    *args: object,
    max_retries: int | None = None,
    base_delay: float | None = None,
    operation_name: str = "operation",
    **kwargs: object,
) -> object:
    """
    Retry an async operation with exponential backoff for transient failures.

    Args:
        coro: Async callable to retry
        *args: Positional arguments for the callable
        max_retries: Maximum retry attempts (default from env or 3)
        base_delay: Initial delay in seconds (default from env or 1.0)
        operation_name: Name for logging purposes
        **kwargs: Keyword arguments for the callable

    Returns:
        Result of the coroutine

    Raises:
        ProxboxException: If all retries fail, with details about each attempt
    """
    max_retries = max_retries if max_retries is not None else _resolve_max_retries()
    base_delay = base_delay if base_delay is not None else _resolve_base_delay()

    errors: list[str] = []
    last_exception: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await coro(*args, **kwargs)
        except Exception as e:
            last_exception = e
            is_transient = _is_transient_netbox_error(e)

            if attempt < max_retries and is_transient:
                delay = base_delay * (2**attempt)
                errors.append(f"Attempt {attempt + 1}/{max_retries + 1} failed: {e}")
                logger.warning(
                    "%s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                    operation_name,
                    attempt + 1,
                    max_retries + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                error_msg = f"{operation_name} failed after {attempt + 1} attempt(s)"
                if attempt < max_retries and not is_transient:
                    error_msg += f" (non-transient error: {e})"
                errors.append(f"Attempt {attempt + 1} failed: {e}")
                logger.error("%s: %s", operation_name, "; ".join(errors))

                detail = f"Failed after {attempt + 1} attempts. Errors: " + "; ".join(errors)
                raise ProxboxException(
                    message=error_msg,
                    detail=detail,
                    python_exception=str(last_exception),
                ) from last_exception

    raise ProxboxException(
        message=f"{operation_name} failed",
        detail="Unexpected: no error was raised but no result returned",
    )


def retry_sync(
    coro: Callable[..., object],
    *args: object,
    max_retries: int | None = None,
    base_delay: float | None = None,
    operation_name: str = "operation",
    **kwargs: object,
) -> object:
    """
    Synchronous wrapper for retry_async using run_coroutine_blocking.
    """
    from proxbox_api.netbox_async_bridge import run_coroutine_blocking

    async def wrapped():
        return await retry_async(
            coro,
            *args,
            max_retries=max_retries,
            base_delay=base_delay,
            operation_name=operation_name,
            **kwargs,
        )

    return run_coroutine_blocking(wrapped())
