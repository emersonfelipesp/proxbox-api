"""Custom exception types and async exception logging helpers."""

from fastapi import WebSocket

from proxbox_api.logger import logger


class ProxboxException(Exception):
    """Base exception for proxbox-api."""

    def __init__(
        self,
        message: str,
        detail: str | dict[str, object] | None = None,
        python_exception: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.python_exception = python_exception

        log_message = f"ProxboxException: {self.message}"

        if self.detail:
            log_message += f"\n > Detail: {self.detail}"

        if self.python_exception:
            log_message += f"\n > Python Exception: {self.python_exception}"


class SyncError(ProxboxException):
    """Base exception for synchronization operations."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        resource_type: str | None = None,
        resource_id: int | str | None = None,
        phase: str | None = None,
        original_error: Exception | None = None,
    ):
        detail = {
            "operation": operation,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "phase": phase,
        }
        detail = {k: v for k, v in detail.items() if v is not None}

        super().__init__(
            message=message,
            detail=detail if detail else None,
            python_exception=str(original_error) if original_error else None,
        )
        self.operation = operation
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.phase = phase
        self.original_error = original_error


class DeviceSyncError(SyncError):
    """Exception raised during device synchronization."""

    def __init__(
        self,
        message: str,
        *,
        device_name: str | None = None,
        cluster: str | None = None,
        phase: str | None = None,
        original_error: Exception | None = None,
    ):
        super().__init__(
            message=message,
            operation="device_sync",
            resource_type="device",
            resource_id=device_name,
            phase=phase,
            original_error=original_error,
        )
        self.device_name = device_name
        self.cluster = cluster


class VMSyncError(SyncError):
    """Exception raised during VM synchronization."""

    def __init__(
        self,
        message: str,
        *,
        vm_id: int | None = None,
        vm_name: str | None = None,
        cluster: str | None = None,
        node: str | None = None,
        phase: str | None = None,
        original_error: Exception | None = None,
    ):
        super().__init__(
            message=message,
            operation="vm_sync",
            resource_type="virtual_machine",
            resource_id=vm_id,
            phase=phase,
            original_error=original_error,
        )
        self.vm_id = vm_id
        self.vm_name = vm_name
        self.cluster = cluster
        self.node = node


class StorageSyncError(SyncError):
    """Exception raised during storage synchronization."""

    def __init__(
        self,
        message: str,
        *,
        storage_name: str | None = None,
        node: str | None = None,
        phase: str | None = None,
        original_error: Exception | None = None,
    ):
        super().__init__(
            message=message,
            operation="storage_sync",
            resource_type="storage",
            resource_id=storage_name,
            phase=phase,
            original_error=original_error,
        )
        self.storage_name = storage_name
        self.node = node


class NetworkSyncError(SyncError):
    """Exception raised during network/interface synchronization."""

    def __init__(
        self,
        message: str,
        *,
        interface_name: str | None = None,
        vm_id: int | None = None,
        phase: str | None = None,
        original_error: Exception | None = None,
    ):
        super().__init__(
            message=message,
            operation="network_sync",
            resource_type="interface",
            resource_id=interface_name,
            phase=phase,
            original_error=original_error,
        )
        self.interface_name = interface_name
        self.vm_id = vm_id


class NetBoxAPIError(ProxboxException):
    """Exception raised when NetBox API operations fail."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        method: str | None = None,
        status_code: int | None = None,
        response_body: str | None = None,
        original_error: Exception | None = None,
    ):
        detail = {
            "endpoint": endpoint,
            "method": method,
            "status_code": status_code,
            "response_body": response_body,
        }
        detail = {k: v for k, v in detail.items() if v is not None}

        super().__init__(
            message=message,
            detail=detail if detail else None,
            python_exception=str(original_error) if original_error else None,
        )
        self.endpoint = endpoint
        self.method = method
        self.status_code = status_code
        self.response_body = response_body
        self.original_error = original_error


class ProxmoxAPIError(ProxboxException):
    """Exception raised when Proxmox API operations fail."""

    def __init__(
        self,
        message: str,
        *,
        node: str | None = None,
        endpoint: str | None = None,
        method: str | None = None,
        status_code: int | None = None,
        original_error: Exception | None = None,
    ):
        detail = {
            "node": node,
            "endpoint": endpoint,
            "method": method,
            "status_code": status_code,
        }
        detail = {k: v for k, v in detail.items() if v is not None}

        super().__init__(
            message=message,
            detail=detail if detail else None,
            python_exception=str(original_error) if original_error else None,
        )
        self.node = node
        self.endpoint = endpoint
        self.method = method
        self.status_code = status_code
        self.original_error = original_error


class ValidationError(ProxboxException):
    """Exception raised when data validation fails."""

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        value: object = None,
        constraint: str | None = None,
        original_error: Exception | None = None,
    ):
        detail = {
            "field": field,
            "value": str(value) if value is not None else None,
            "constraint": constraint,
        }
        detail = {k: v for k, v in detail.items() if v is not None}

        super().__init__(
            message=message,
            detail=detail if detail else None,
            python_exception=str(original_error) if original_error else None,
        )
        self.field = field
        self.value = value
        self.constraint = constraint
        self.original_error = original_error


async def exception_log(
    message: str,
    detail: str | None = None,
    python_exception: str | None = None,
    websocket: WebSocket | None = None,
) -> None:
    """Log an exception with optional WebSocket notification.

    Args:
        message: Error message to log
        detail: Additional detail about the error
        python_exception: Python exception string representation
        websocket: Optional WebSocket connection to send error notification
    """
    # Log the error to console
    logger.error(
        message,
        extra={
            "detail": detail,
            "python_exception": python_exception,
            "websocket_id": id(websocket) if websocket else None,
        },
    )

    # Also send to WebSocket if available
    if websocket:
        from proxbox_api.utils.websocket_utils import send_error

        await send_error(websocket, message, context=detail)
