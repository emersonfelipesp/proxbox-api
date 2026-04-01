"""Comprehensive error handling improvements for sync operations."""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar, Callable, ParamSpec
from functools import wraps

from proxbox_api.exception import (
    SyncError,
    VMSyncError,
    DeviceSyncError,
    StorageSyncError,
    NetworkSyncError,
    ValidationError,
    ProxmoxAPIError,
    NetBoxAPIError,
)
from proxbox_api.logger import logger

P = ParamSpec("P")
R = TypeVar("R")


def validate_netbox_response(response: Any, operation: str) -> Any:
    """Validate a NetBox response and raise typed error if invalid.

    Args:
        response: Response from NetBox
        operation: Operation name for error context

    Returns:
        The response if valid

    Raises:
        NetBoxAPIError: If response is invalid or missing required fields
    """
    if response is None:
        raise NetBoxAPIError(
            message=f"NetBox {operation} returned None",
            endpoint=operation,
        )
    if isinstance(response, dict):
        if "id" not in response or response["id"] is None:
            raise NetBoxAPIError(
                message=f"NetBox {operation} response missing ID field",
                endpoint=operation,
                response_body=str(response),
            )
    return response


def validate_proxmox_response(response: Any, operation: str, node: str | None = None) -> Any:
    """Validate a Proxmox response and raise typed error if invalid.

    Args:
        response: Response from Proxmox
        operation: Operation name for error context
        node: Optional node name for context

    Returns:
        The response if valid

    Raises:
        ProxmoxAPIError: If response is invalid or missing required fields
    """
    if response is None:
        raise ProxmoxAPIError(
            message=f"Proxmox {operation} returned None",
            endpoint=operation,
            node=node,
        )
    return response


def with_sync_error_handling(
    operation: str,
    resource_type: str | None = None,
    phase: str | None = None,
    error_class: type[SyncError] = SyncError,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator to wrap sync operations with error handling and logging.

    Catches exceptions, logs context, and raises typed SyncError.

    Args:
        operation: Name of operation ("vm_creation", "device_sync", etc.)
        resource_type: Type of resource being synced
        phase: Phase of sync operation
        error_class: Exception class to raise (VMSyncError, DeviceSyncError, etc.)

    Returns:
        Decorated function
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
                raise error_class(
                    message=f"Failed {operation}",
                    operation=operation,
                    resource_type=resource_type,
                    phase=phase,
                    original_error=e,
                ) from e

        return wrapper  # type: ignore

    return decorator


def with_async_sync_error_handling(
    operation: str,
    resource_type: str | None = None,
    phase: str | None = None,
    error_class: type[SyncError] = SyncError,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Async version of with_sync_error_handling decorator."""

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await func(*args, **kwargs)
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
                raise error_class(
                    message=f"Failed {operation}",
                    operation=operation,
                    resource_type=resource_type,
                    phase=phase,
                    original_error=e,
                ) from e

        return wrapper  # type: ignore

    return decorator


def with_retry(
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator to retry a function with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts
        backoff_seconds: Initial backoff in seconds
        exponential_base: Multiplier for each retry (2.0 = double each time)
        retryable_exceptions: Tuple of exceptions to retry on

    Returns:
        Decorated function
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_error: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_error = e
                    if attempt < max_attempts:
                        wait_time = backoff_seconds * (exponential_base ** (attempt - 1))
                        logger.debug(
                            f"Attempt {attempt}/{max_attempts} failed, retrying in {wait_time}s: {e}"
                        )
                        import time

                        time.sleep(wait_time)
                    else:
                        logger.error(
                            f"All {max_attempts} attempts failed for {func.__name__}: {e}",
                            exc_info=True,
                        )
            raise last_error or Exception(f"Failed after {max_attempts} attempts")

        return wrapper  # type: ignore

    return decorator


def with_async_retry(
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Async version of with_retry decorator."""

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_error: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_error = e
                    if attempt < max_attempts:
                        wait_time = backoff_seconds * (exponential_base ** (attempt - 1))
                        logger.debug(
                            f"Attempt {attempt}/{max_attempts} failed, retrying in {wait_time}s: {e}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"All {max_attempts} attempts failed for {func.__name__}: {e}",
                            exc_info=True,
                        )
            raise last_error or Exception(f"Failed after {max_attempts} attempts")

        return wrapper  # type: ignore

    return decorator


class EarlyReturnContext:
    """Context manager for early returns with cleanup.

    Usage:
        async with EarlyReturnContext("operation_name") as ctx:
            if invalid_condition:
                ctx.early_return("Operation skipped due to validation")
            # ... rest of operation
    """

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.should_return = False
        self.return_message = ""

    async def __aenter__(self) -> EarlyReturnContext:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if self.should_return:
            logger.info(f"{self.operation}: {self.return_message}")
            return True
        return False

    def early_return(self, message: str) -> None:
        """Mark for early return with message."""
        self.should_return = True
        self.return_message = message
