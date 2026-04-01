"""Tests for structured logging utilities."""

from unittest.mock import MagicMock, patch

from proxbox_api.utils.structured_logging import (
    SyncPhaseLogger,
    log_sync_operation,
    log_sync_result,
)


class TestSyncPhaseLogger:
    """Tests for SyncPhaseLogger context manager and methods."""

    def test_init(self) -> None:
        """Test logger initialization."""
        logger = SyncPhaseLogger("test_op", vm_id=123, cluster="prod")
        assert logger.operation == "test_op"
        assert logger.base_context["operation"] == "test_op"
        assert logger.base_context["vm_id"] == 123
        assert logger.base_context["cluster"] == "prod"

    @patch("proxbox_api.utils.structured_logging.logger")
    def test_log_phase_info(self, mock_logger: MagicMock) -> None:
        """Test logging a phase with info level."""
        logger = SyncPhaseLogger("test_op", resource_id=1)
        logger.log_phase("filtering", "Starting filter phase")

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert "[filtering] Starting filter phase" in str(call_args)

    @patch("proxbox_api.utils.structured_logging.logger")
    def test_log_phase_warning(self, mock_logger: MagicMock) -> None:
        """Test logging a phase with warning level."""
        logger = SyncPhaseLogger("test_op")
        logger.log_phase("validation", "Validation issue", level="warning")

        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert "[validation] Validation issue" in str(call_args)

    @patch("proxbox_api.utils.structured_logging.logger")
    def test_log_phase_complete(self, mock_logger: MagicMock) -> None:
        """Test logging phase completion with metrics."""
        logger = SyncPhaseLogger("vm_sync", vm_id=456)
        logger.log_phase_complete("creation", "Phase complete", resource_count=10)

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert "[creation] Phase complete" in str(call_args)
        assert call_args[1]["extra"]["resource_count"] == 10

    @patch("proxbox_api.utils.structured_logging.logger")
    def test_log_resource(self, mock_logger: MagicMock) -> None:
        """Test logging a resource-specific event."""
        logger = SyncPhaseLogger("device_sync", cluster="prod")
        logger.log_resource("creation", "vm", 123, "VM created successfully", success=True)

        mock_logger.debug.assert_called_once()
        call_args = mock_logger.debug.call_args
        assert "[creation] vm#123" in str(call_args)

    @patch("proxbox_api.utils.structured_logging.logger")
    def test_log_error(self, mock_logger: MagicMock) -> None:
        """Test logging an error with context."""
        logger = SyncPhaseLogger("test_op")
        error = ValueError("Test error")
        logger.log_error("processing", "Operation failed", error, error_code=500)

        mock_logger.error.assert_called_once()
        call_args = mock_logger.error.call_args
        assert "[processing] Operation failed" in str(call_args)
        assert call_args[1]["extra"]["error_type"] == "ValueError"
        assert call_args[1]["extra"]["error_code"] == 500


class TestLogSyncOperation:
    """Tests for log_sync_operation function."""

    @patch("proxbox_api.utils.structured_logging.logger")
    def test_log_sync_operation(self, mock_logger: MagicMock) -> None:
        """Test logging sync operation start."""
        log_sync_operation("device_sync", cluster="prod", total=10)

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert "Starting sync operation: device_sync" in str(call_args)
        assert call_args[1]["extra"]["cluster"] == "prod"
        assert call_args[1]["extra"]["total"] == 10


class TestLogSyncResult:
    """Tests for log_sync_result function."""

    @patch("proxbox_api.utils.structured_logging.logger")
    def test_log_sync_result_success(self, mock_logger: MagicMock) -> None:
        """Test logging successful sync result."""
        log_sync_result("device_sync", success_count=10, failure_count=0, elapsed_seconds=5.5)

        mock_logger.log.assert_called_once()
        call_args = mock_logger.log.call_args
        assert "completed successfully" in str(call_args)
        assert call_args[1]["extra"]["success_count"] == 10
        assert call_args[1]["extra"]["total"] == 10

    @patch("proxbox_api.utils.structured_logging.logger")
    def test_log_sync_result_with_failures(self, mock_logger: MagicMock) -> None:
        """Test logging sync result with failures."""
        log_sync_result("vm_sync", success_count=8, failure_count=2, elapsed_seconds=3.2)

        mock_logger.log.assert_called_once()
        call_args = mock_logger.log.call_args
        assert "completed with issues" in str(call_args)
        assert call_args[1]["extra"]["failure_count"] == 2
        assert call_args[1]["extra"]["total"] == 10
