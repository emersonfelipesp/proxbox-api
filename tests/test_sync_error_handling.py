"""Tests for sync error handling decorators and validators."""

from unittest.mock import MagicMock, patch

import pytest

from proxbox_api.exception import NetBoxAPIError, ProxmoxAPIError, SyncError
from proxbox_api.utils.sync_error_handling import (
    EarlyReturnContext,
    validate_netbox_response,
    validate_proxmox_response,
    with_async_retry,
    with_async_sync_error_handling,
    with_retry,
    with_sync_error_handling,
)


class TestValidateResponses:
    """Tests for response validators."""

    def test_validate_netbox_response_valid(self) -> None:
        """Test validation of valid NetBox response."""
        response = {"id": 123, "name": "test"}
        result = validate_netbox_response(response, "test_op")
        assert result == response

    def test_validate_netbox_response_none(self) -> None:
        """Test validation fails for None response."""
        with pytest.raises(NetBoxAPIError):
            validate_netbox_response(None, "test_op")

    def test_validate_netbox_response_missing_id(self) -> None:
        """Test validation fails when ID is missing."""
        with pytest.raises(NetBoxAPIError):
            validate_netbox_response({"name": "test"}, "test_op")

    def test_validate_proxmox_response_valid(self) -> None:
        """Test validation of valid Proxmox response."""
        response = {"data": "test"}
        result = validate_proxmox_response(response, "test_op")
        assert result == response

    def test_validate_proxmox_response_none(self) -> None:
        """Test validation fails for None response."""
        with pytest.raises(ProxmoxAPIError):
            validate_proxmox_response(None, "test_op", node="node1")


class TestSyncErrorHandlingDecorator:
    """Tests for sync error handling decorator."""

    @patch("proxbox_api.utils.sync_error_handling.logger")
    def test_sync_error_handling_success(self, mock_logger: MagicMock) -> None:
        """Test decorator on successful operation."""

        @with_sync_error_handling("test_operation")
        def test_func() -> str:
            return "success"

        result = test_func()
        assert result == "success"

    @patch("proxbox_api.utils.sync_error_handling.logger")
    def test_sync_error_handling_failure(self, mock_logger: MagicMock) -> None:
        """Test decorator catches exception and re-raises as SyncError."""

        @with_sync_error_handling("test_operation", resource_type="vm")
        def test_func() -> str:
            raise ValueError("Test error")

        with pytest.raises(SyncError):
            test_func()

        mock_logger.error.assert_called_once()


class TestAsyncSyncErrorHandlingDecorator:
    """Tests for async sync error handling decorator."""

    @pytest.mark.asyncio
    @patch("proxbox_api.utils.sync_error_handling.logger")
    async def test_async_sync_error_handling_success(self, mock_logger: MagicMock) -> None:
        """Test async decorator on successful operation."""

        @with_async_sync_error_handling("test_operation")
        async def test_func() -> str:
            return "success"

        result = await test_func()
        assert result == "success"

    @pytest.mark.asyncio
    @patch("proxbox_api.utils.sync_error_handling.logger")
    async def test_async_sync_error_handling_failure(self, mock_logger: MagicMock) -> None:
        """Test async decorator catches exception and re-raises as SyncError."""

        @with_async_sync_error_handling("test_operation")
        async def test_func() -> str:
            raise ValueError("Test error")

        with pytest.raises(SyncError):
            await test_func()

        mock_logger.error.assert_called_once()


class TestRetryDecorator:
    """Tests for retry decorator."""

    @patch("time.sleep")
    def test_retry_success_first_attempt(self, mock_sleep: MagicMock) -> None:
        """Test retry succeeds on first attempt."""
        call_count = 0

        @with_retry(max_attempts=3, backoff_seconds=0.1)
        def test_func() -> str:
            nonlocal call_count
            call_count += 1
            return "success"

        result = test_func()
        assert result == "success"
        assert call_count == 1

    @patch("time.sleep")
    def test_retry_success_after_failures(self, mock_sleep: MagicMock) -> None:
        """Test retry succeeds after initial failures."""
        call_count = 0

        @with_retry(max_attempts=3, backoff_seconds=0.1)
        def test_func() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Temporary error")
            return "success"

        result = test_func()
        assert result == "success"
        assert call_count == 3

    @patch("time.sleep")
    def test_retry_exhausted(self, mock_sleep: MagicMock) -> None:
        """Test retry exhaustion raises error."""

        @with_retry(max_attempts=2, backoff_seconds=0.1)
        def test_func() -> str:
            raise ValueError("Persistent error")

        with pytest.raises(ValueError):
            test_func()


class TestAsyncRetryDecorator:
    """Tests for async retry decorator."""

    @pytest.mark.asyncio
    @patch("proxbox_api.utils.sync_error_handling.logger")
    async def test_async_retry_success(self, mock_logger: MagicMock) -> None:
        """Test async retry succeeds on first attempt."""

        @with_async_retry(max_attempts=3, backoff_seconds=0.01)
        async def test_func() -> str:
            return "success"

        result = await test_func()
        assert result == "success"

    @pytest.mark.asyncio
    @patch("proxbox_api.utils.sync_error_handling.logger")
    async def test_async_retry_exhausted(self, mock_logger: MagicMock) -> None:
        """Test async retry exhaustion."""

        @with_async_retry(max_attempts=2, backoff_seconds=0.01)
        async def test_func() -> str:
            raise ValueError("Error")

        with pytest.raises(ValueError):
            await test_func()


class TestEarlyReturnContext:
    """Tests for EarlyReturnContext manager."""

    @pytest.mark.asyncio
    async def test_early_return_context_no_return(self) -> None:
        """Test context completes normally."""
        executed = False

        async with EarlyReturnContext("test"):
            executed = True
            # No early return

        assert executed

    @pytest.mark.asyncio
    async def test_early_return_context_with_return(self) -> None:
        """Test context with early return."""
        executed_after = False

        async with EarlyReturnContext("test") as ctx:
            ctx.early_return("Skipping operation")
            executed_after = True

        # Should not reach here due to suppressed exception
        assert not executed_after or ctx.should_return
