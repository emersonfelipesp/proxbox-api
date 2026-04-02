"""Error handling utilities and decorators for sync operations."""

from __future__ import annotations

from functools import wraps
from typing import Callable, ParamSpec, TypeVar

from proxbox_api.exception import SyncError
from proxbox_api.logger import logger

P = ParamSpec("P")
R = TypeVar("R")


def handle_sync_error(
    operation: str,
    resource_type: str | None = None,
    phase: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator to wrap sync operation errors with context.

    Usage:
        @handle_sync_error("vm_creation", resource_type="virtual_machine", phase="network")
        async def sync_network():
            ...
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return func(*args, **kwargs)
            except SyncError:
                raise
            except Exception as e:
                logger.error(
                    f"Error during {operation}: {e}",
                    exc_info=True,
                    extra={
                        "operation": operation,
                        "resource_type": resource_type,
                        "phase": phase,
                    },
                )
                raise SyncError(
                    message=f"Failed {operation}",
                    operation=operation,
                    resource_type=resource_type,
                    phase=phase,
                    original_error=e,
                )

        return wrapper  # type: ignore

    return decorator


def early_return_if_none(value: object, message: str) -> None:
    """Helper to raise error and exit early if value is None."""
    if value is None:
        raise ValueError(message)


def early_return_if_empty(value: list | dict, message: str) -> None:
    """Helper to raise error and exit early if collection is empty."""
    if not value:
        raise ValueError(message)


def early_return_if_invalid_id(value: int | None, message: str) -> int:
    """Helper to raise error and exit early if ID is invalid."""
    if value is None or value <= 0:
        raise ValueError(message)
    return value


def safe_getattr(obj: object, attr: str, default: object = None, raise_on_none: bool = False) -> object:
    """Safely get attribute with optional early return on None.

    Args:
        obj: Object to get attribute from
        attr: Attribute name
        default: Default value if attribute missing
        raise_on_none: If True, raise error when value is None

    Returns:
        Attribute value or default

    Raises:
        ValueError: If raise_on_none is True and value is None
    """
    value = getattr(obj, attr, default)
    if raise_on_none and value is None:
        raise ValueError(f"Required attribute '{attr}' is None")
    return value
