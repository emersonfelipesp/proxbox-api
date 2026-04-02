"""WebSocket messaging utilities for standardized communication."""

from fastapi import WebSocket

from proxbox_api.logger import logger


async def send_progress_update(
    websocket: WebSocket | None,
    message: str,
    *,
    progress: int | None = None,
    total: int | None = None,
    current_item: str | None = None,
    level: str = "INFO",
    use_css: bool = False,
) -> None:
    """Send a standardized progress update via WebSocket.

    Args:
        websocket: WebSocket connection (None = skip sending)
        message: Progress message
        progress: Current progress count
        total: Total item count
        current_item: Name/identifier of current item being processed
        level: Log level (INFO, WARNING, ERROR, DEBUG)
        use_css: Whether to include CSS styling
    """
    if websocket is None:
        return

    try:
        payload: dict[str, object] = {
            "message": message,
            "level": level,
        }

        if progress is not None:
            payload["progress"] = progress

        if total is not None:
            payload["total"] = total

        if current_item is not None:
            payload["current_item"] = current_item

        if use_css:
            payload["use_css"] = True

        await websocket.send_json(payload)

    except Exception as error:
        logger.warning(
            f"Failed to send WebSocket progress update: {error}",
            extra={"websocket_id": id(websocket)},
        )


async def send_status_message(
    websocket: WebSocket | None,
    message: str,
    *,
    status: str = "info",
    use_css: bool = False,
) -> None:
    """Send a status message via WebSocket.

    Args:
        websocket: WebSocket connection
        message: Status message
        status: Status type (info, success, warning, error)
        use_css: Whether to include CSS styling
    """
    if websocket is None:
        return

    level_map = {
        "info": "INFO",
        "success": "INFO",
        "warning": "WARNING",
        "error": "ERROR",
    }

    await send_progress_update(
        websocket=websocket,
        message=message,
        level=level_map.get(status, "INFO"),
        use_css=use_css,
    )


async def send_phase_start(
    websocket: WebSocket | None,
    phase: str,
    *,
    description: str | None = None,
    use_css: bool = False,
) -> None:
    """Send a phase start notification.

    Args:
        websocket: WebSocket connection
        phase: Phase name
        description: Optional phase description
        use_css: Whether to include CSS styling
    """
    message = f"Starting phase: {phase}"
    if description:
        message += f" - {description}"

    await send_status_message(
        websocket=websocket,
        message=message,
        status="info",
        use_css=use_css,
    )


async def send_phase_complete(
    websocket: WebSocket | None,
    phase: str,
    *,
    success: bool = True,
    use_css: bool = False,
) -> None:
    """Send a phase completion notification.

    Args:
        websocket: WebSocket connection
        phase: Phase name
        success: Whether the phase completed successfully
        use_css: Whether to include CSS styling
    """
    status = "success" if success else "error"
    message = f"{'Completed' if success else 'Failed'} phase: {phase}"

    await send_status_message(
        websocket=websocket,
        message=message,
        status=status,
        use_css=use_css,
    )


async def send_error(
    websocket: WebSocket | None,
    error: Exception | str,
    *,
    context: str | None = None,
    use_css: bool = False,
) -> None:
    """Send an error notification via WebSocket.

    Args:
        websocket: WebSocket connection
        error: Exception or error message
        context: Optional context about where the error occurred
        use_css: Whether to include CSS styling
    """
    if websocket is None:
        return

    if isinstance(error, Exception):
        error_msg = f"{type(error).__name__}: {str(error)}"
    else:
        error_msg = str(error)

    if context:
        error_msg = f"{context}: {error_msg}"

    await send_status_message(
        websocket=websocket,
        message=error_msg,
        status="error",
        use_css=use_css,
    )


async def send_summary(
    websocket: WebSocket | None,
    *,
    created: int = 0,
    updated: int = 0,
    deleted: int = 0,
    failed: int = 0,
    skipped: int = 0,
    total: int | None = None,
    operation: str | None = None,
    use_css: bool = False,
) -> None:
    """Send a summary of sync operation results.

    Args:
        websocket: WebSocket connection
        created: Number of items created
        updated: Number of items updated
        deleted: Number of items deleted
        failed: Number of items that failed
        skipped: Number of items skipped
        total: Total number of items processed
        operation: Name of the operation
        use_css: Whether to include CSS styling
    """
    if websocket is None:
        return

    parts = []
    if created > 0:
        parts.append(f"{created} created")
    if updated > 0:
        parts.append(f"{updated} updated")
    if deleted > 0:
        parts.append(f"{deleted} deleted")
    if failed > 0:
        parts.append(f"{failed} failed")
    if skipped > 0:
        parts.append(f"{skipped} skipped")

    summary = ", ".join(parts) if parts else "No changes"

    message = "Summary"
    if operation:
        message += f" for {operation}"
    message += f": {summary}"

    if total is not None:
        message += f" (total: {total})"

    status = "error" if failed > 0 else "success"

    await send_status_message(
        websocket=websocket,
        message=message,
        status=status,
        use_css=use_css,
    )
