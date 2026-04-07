"""Retry utilities for handling transient connection failures."""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger

T = TypeVar("T")

DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_DELAY = 2.0


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


def _is_netbox_overwhelmed_error(error: Exception) -> bool:
    """Check whether NetBox appears overloaded or PostgreSQL-saturated."""
    details: list[str] = [str(error)]
    for attr_name in ("detail", "python_exception"):
        attr_val = getattr(error, attr_name, None)
        if attr_val is not None:
            details.append(str(attr_val))
    error_str = " ".join(details).lower()
    overload_indicators = [
        "too many connections",
        "remaining connection slots",
        "connection slots are reserved",
        "database unavailable",
        "service unavailable",
        "service temporarily unavailable",
        "http 503",
        "status 503",
        "operationalerror",
        "psycopg2.errors",
    ]
    return any(indicator in error_str for indicator in overload_indicators)


def _is_connection_refused_error(error: Exception) -> bool:
    """Check if this is a connection refused error (NetBox completely unreachable)."""
    error_str = str(error).lower()
    return "connection refused" in error_str or "connect call failed" in error_str


def _compute_delay(
    attempt: int,
    base_delay: float,
    is_connection_refused: bool = False,
    is_overwhelmed: bool = False,
) -> float:
    """Compute delay with exponential backoff and jitter.

    For connection refused errors (NetBox offline), use longer delays since
    retrying immediately won't help - NetBox needs time to come back.
    For overwhelmed errors (DB pool saturated), use aggressive backoff.
    """
    exponential_delay = base_delay * (2**attempt)
    if is_connection_refused:
        exponential_delay = max(exponential_delay, 10.0)
    if is_overwhelmed:
        exponential_delay = max(exponential_delay, 30.0)
    jitter = random.uniform(0, exponential_delay * 0.5)
    return exponential_delay + jitter


async def retry_async(
    coro: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int | None = None,
    base_delay: float | None = None,
    operation_name: str = "operation",
    **kwargs: Any,
) -> T:
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
            can_retry = attempt < max_retries and is_transient
            is_final = not can_retry

            if is_final:
                error_msg = f"{operation_name} failed after {attempt + 1} attempt(s)"
                if not is_transient:
                    error_msg += f" (non-transient error: {e})"
                errors.append(f"Attempt {attempt + 1} failed: {e}")
                logger.error("%s: %s", operation_name, "; ".join(errors))
                detail = f"Failed after {attempt + 1} attempts. Errors: " + "; ".join(errors)
                raise ProxboxException(
                    message=error_msg,
                    detail=detail,
                    python_exception=str(last_exception),
                ) from last_exception

            is_conn_refused = _is_connection_refused_error(e)
            is_overwhelmed = _is_netbox_overwhelmed_error(e)
            delay = _compute_delay(attempt, base_delay, is_conn_refused, is_overwhelmed)
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

    raise ProxboxException(
        message=f"{operation_name} failed",
        detail="Unexpected: no error was raised but no result returned",
    )
