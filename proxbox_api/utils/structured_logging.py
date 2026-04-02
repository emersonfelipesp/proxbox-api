"""Structured logging enhancements for sync operations."""

from __future__ import annotations

from proxbox_api.logger import get_operation_context, logger


class SyncPhaseLogger:
    """Helper for logging distinct phases of sync operations with context tracking.

    Usage:
        phase_logger = SyncPhaseLogger("vm_sync", vm_id=123)
        phase_logger.log_phase("filtering", "Starting cluster resource filtering")
        # ... filtering logic ...
        phase_logger.log_phase_complete("filtering", resource_count=5)
        phase_logger.log_phase("creation", "Starting VM creation")
        # ... creation logic ...
        phase_logger.log_phase_complete("creation", created_count=5)
    """

    def __init__(self, operation: str, **context: object) -> None:
        """Initialize sync phase logger.

        Args:
            operation: Name of the overall operation (vm_sync, device_sync, etc.)
            **context: Initial context fields (vm_id, cluster, etc.)
        """
        self.operation = operation
        self.base_context = {"operation": operation, **context}

    def log_phase(
        self,
        phase: str,
        message: str,
        level: str = "info",
        **extra: object,
    ) -> None:
        """Log the start of a phase with context.

        Args:
            phase: Phase name (filtering, creation, validation, etc.)
            message: Log message
            level: Log level (debug, info, warning, error)
            **extra: Additional context fields
        """
        context = {**self.base_context, "phase": phase, **extra}
        self._log(level, f"[{phase}] {message}", context)

    def log_phase_complete(
        self,
        phase: str,
        message: str = "Phase completed",
        level: str = "info",
        **metrics: object,
    ) -> None:
        """Log the completion of a phase with metrics.

        Args:
            phase: Phase name
            message: Completion message
            level: Log level
            **metrics: Metric fields (count, duration, errors, etc.)
        """
        context = {**self.base_context, "phase": phase, **metrics}
        self._log(level, f"[{phase}] {message}", context)

    def log_resource(
        self,
        phase: str,
        resource_type: str,
        resource_id: str | int,
        message: str,
        level: str = "debug",
        **extra: object,
    ) -> None:
        """Log an event related to a specific resource.

        Args:
            phase: Current phase
            resource_type: Type of resource (vm, device, interface, etc.)
            resource_id: Resource identifier
            message: Log message
            level: Log level
            **extra: Additional context
        """
        context = {
            **self.base_context,
            "phase": phase,
            "resource_type": resource_type,
            "resource_id": resource_id,
            **extra,
        }
        self._log(level, f"[{phase}] {resource_type}#{resource_id}: {message}", context)

    def log_error(
        self,
        phase: str,
        message: str,
        error: Exception,
        **context_data: object,
    ) -> None:
        """Log an error with full context and exception details.

        Args:
            phase: Current phase
            message: Error message
            error: The exception that occurred
            **context_data: Additional context fields
        """
        context = {
            **self.base_context,
            "phase": phase,
            "error_type": type(error).__name__,
            **context_data,
        }
        logger.error(f"[{phase}] {message}: {error}", exc_info=True, extra=context)

    def _log(self, level: str, message: str, context: dict[str, object]) -> None:
        """Internal logging method.

        Args:
            level: Log level name
            message: Log message
            context: Context dict to pass to logger
        """
        log_func = getattr(logger, level.lower(), logger.info)
        log_func(message, extra=context)


def log_sync_operation(operation: str, **context: object) -> None:
    """Log the start of a sync operation with context.

    Args:
        operation: Operation name
        **context: Context fields
    """
    full_context = {**context, "operation": operation}
    logger.info(f"Starting sync operation: {operation}", extra=full_context)


def log_sync_result(
    operation: str,
    success_count: int,
    failure_count: int,
    elapsed_seconds: float = 0.0,
    **extra: object,
) -> None:
    """Log the result of a sync operation with counts.

    Args:
        operation: Operation name
        success_count: Number of successful items
        failure_count: Number of failed items
        elapsed_seconds: Total time elapsed
        **extra: Additional context
    """
    context = {
        **extra,
        "operation": operation,
        "success_count": success_count,
        "failure_count": failure_count,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "total": success_count + failure_count,
    }
    level = "warning" if failure_count > 0 else "info"
    status = "completed with issues" if failure_count > 0 else "completed successfully"
    logger.log(
        logger.getLevelName(level.upper()),
        f"Sync operation {operation} {status}: {success_count} successful, {failure_count} failed",
        extra=context,
    )


def get_current_sync_context() -> dict[str, object]:
    """Get the current operation context.

    Returns:
        Dictionary with operation context fields
    """
    return get_operation_context()
