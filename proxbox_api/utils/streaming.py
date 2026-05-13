"""Helpers for server-sent event streaming in sync endpoints."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable

from proxbox_api.schemas.stream_messages import (
    ErrorCategory,
    ItemOperation,
    SubstepStatus,
    build_discovery_message,
    build_duplicate_name_resolved_message,
    build_error_detail_message,
    build_hardware_discovery_message,
    build_item_progress_message,
    build_phase_summary_message,
    build_substep_message,
)


def _to_serializable(obj: object) -> object:
    """Recursively convert RestRecord and similar objects to JSON-serializable dicts."""
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_serializable(item) for item in obj]
    if hasattr(obj, "serialize") and callable(obj.serialize):
        return _to_serializable(obj.serialize())
    if hasattr(obj, "dict") and callable(obj.dict):
        return _to_serializable(obj.dict())
    return obj


def sse_event(event: str, data: object) -> str:
    """Serialize one SSE frame."""
    return f"event: {event}\ndata: {json.dumps(_to_serializable(data))}\n\n"


class WebSocketSSEBridge:
    """Compatibility bridge that turns websocket-like JSON payloads into SSE frames.

    This class supports both legacy message formats (object/type/data) and new
    structured message types (discovery, substep, item_progress, phase_summary, error_detail).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict[str, object]] | None] = asyncio.Queue()
        self._timing_start: dict[str, float] = {}

    # Timing tracking

    def start_timer(self, key: str) -> None:
        """Start timing an operation."""
        self._timing_start[key] = time.time()

    def get_elapsed_ms(self, key: str) -> int | None:
        """Get elapsed time in milliseconds for a started operation."""
        start = self._timing_start.get(key)
        if start is None:
            return None
        return int((time.time() - start) * 1000)

    def clear_timer(self, key: str) -> None:
        """Clear a timing entry."""
        self._timing_start.pop(key, None)

    # Legacy send_json support

    async def send_json(self, payload: dict[str, object]) -> None:
        """Support sync services that call ``await websocket.send_json(...)``."""
        object_name = str(payload.get("object") or "sync")
        row_id = self._extract_row_id(payload)
        status = "completed" if payload.get("end") is True else self._extract_status(payload)
        message = self._build_message(object_name, payload, row_id, status)
        await self.emit(
            "step",
            {
                "step": object_name,
                "status": status,
                "message": message,
                "rowid": row_id,
                "payload": payload,
            },
        )

    async def emit(self, event: str, data: dict[str, object]) -> None:
        await self._queue.put((event, data))

    async def close(self) -> None:
        await self._queue.put(None)

    async def iter_sse(self, keepalive_interval: float = 15.0) -> AsyncIterator[str]:
        """Yield SSE frames from the queue, emitting keepalive comments when idle.

        SSE comment lines (starting with ``:``) are ignored by all spec-compliant
        consumers including the plugin's ``_iter_sse_frames`` parser, so keepalives
        are transparent to callers.  The interval prevents intermediate proxies from
        closing a long-running stream when the sync task is doing heavy work and not
        emitting progress events.
        """
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=keepalive_interval)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is None:
                break
            event, data = item
            yield sse_event(event, data)

    # New structured message emission methods

    async def emit_discovery(
        self,
        phase: str,
        items: list[dict[str, object]],
        message: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Emit a discovery message listing items to be processed.

        Args:
            phase: Phase name (e.g., 'devices', 'virtual-machines')
            items: List of items discovered, each with at least 'name' key
            message: Optional custom message
            metadata: Optional additional metadata
        """
        msg = build_discovery_message(
            phase=phase,
            items=items,
            message=message,
            metadata=metadata,
        )
        await self._queue.put(("discovery", msg))

    async def emit_substep(
        self,
        phase: str,
        substep: str,
        status: SubstepStatus | str,
        message: str,
        item: dict[str, object] | None = None,
        timing_key: str | None = None,
        result: dict[str, object] | None = None,
    ) -> None:
        """Emit a substep message for granular operations.

        Args:
            phase: Phase name
            substep: Substep identifier (e.g., 'ensure_cluster', 'create_device')
            status: Substep status
            message: Human-readable message
            item: Optional item context
            timing_key: Optional timing key to calculate elapsed time
            result: Optional result data
        """
        status_val = status if isinstance(status, str) else status.value
        timing_ms = None
        if timing_key:
            timing_ms = self.get_elapsed_ms(timing_key)
        msg = build_substep_message(
            phase=phase,
            substep=substep,
            status=status_val,
            message=message,
            item=item,
            timing_ms=timing_ms,
            result=result,
        )
        await self._queue.put(("substep", msg))

    async def emit_item_progress(
        self,
        phase: str,
        item: dict[str, object],
        operation: ItemOperation | str,
        status: str,
        message: str,
        progress_current: int,
        progress_total: int,
        timing_key: str | None = None,
        error: str | None = None,
        warning: str | None = None,
    ) -> None:
        """Emit an item progress message.

        Args:
            phase: Phase name
            item: Item being processed with at least 'name' key
            operation: Operation type (created, updated, deleted, skipped, failed)
            status: Item status (processing, completed, failed)
            message: Human-readable message
            progress_current: Current item number (1-indexed)
            progress_total: Total number of items
            timing_key: Optional timing key for elapsed time
            error: Optional error message
            warning: Optional warning message
        """
        op_val = operation if isinstance(operation, str) else operation.value
        timing_ms = None
        if timing_key:
            timing_ms = self.get_elapsed_ms(timing_key)
        msg = build_item_progress_message(
            phase=phase,
            item=item,
            operation=op_val,
            status=status,
            message=message,
            progress_current=progress_current,
            progress_total=progress_total,
            timing_ms=timing_ms,
            error=error,
            warning=warning,
        )
        await self._queue.put(("item_progress", msg))

    async def emit_phase_summary(
        self,
        phase: str,
        created: int = 0,
        updated: int = 0,
        deleted: int = 0,
        failed: int = 0,
        skipped: int = 0,
        timing_key: str | None = None,
        message: str | None = None,
    ) -> None:
        """Emit a phase summary message at the end of a sync phase.

        Args:
            phase: Phase name
            created: Number of items created
            updated: Number of items updated
            deleted: Number of items deleted
            failed: Number of items failed
            skipped: Number of items skipped
            timing_key: Optional timing key for elapsed time
            message: Optional custom message
        """
        timing_ms = None
        if timing_key:
            timing_ms = self.get_elapsed_ms(timing_key)
        msg = build_phase_summary_message(
            phase=phase,
            created=created,
            updated=updated,
            deleted=deleted,
            failed=failed,
            skipped=skipped,
            timing_ms=timing_ms,
            message=message,
        )
        await self._queue.put(("phase_summary", msg))

    async def emit_error_detail(
        self,
        message: str,
        category: ErrorCategory | str,
        phase: str | None = None,
        item: dict[str, object] | None = None,
        detail: str | None = None,
        suggestion: str | None = None,
        traceback: str | None = None,
    ) -> None:
        """Emit a detailed error message with categorization and remediation hints.

        Args:
            message: Human-readable error message
            category: Error category
            phase: Optional phase where error occurred
            item: Optional item associated with error
            detail: Optional technical error details
            suggestion: Optional remediation suggestion
            traceback: Optional stack trace (debug mode only)
        """
        cat_val = category if isinstance(category, str) else category.value
        msg = build_error_detail_message(
            message=message,
            category=cat_val,
            phase=phase,
            item=item,
            detail=detail,
            suggestion=suggestion,
            traceback=traceback,
        )
        await self._queue.put(("error_detail", msg))

    async def emit_duplicate_name_resolved(
        self,
        cluster: str,
        original_name: str,
        resolved_name: str,
        vmid: int,
        suffix_index: int,
        operator_renamed: bool = False,
    ) -> None:
        """Emit a `duplicate_name_resolved` warning frame.

        Args:
            cluster: Proxmox cluster name (human label)
            original_name: Candidate VM name from Proxmox
            resolved_name: Final name written to NetBox
            vmid: Proxmox VMID
            suffix_index: 1 = no algorithmic suffix (operator-rename); 2+ = suffix applied
            operator_renamed: True when the NetBox record was already manually renamed
        """
        msg = build_duplicate_name_resolved_message(
            cluster=cluster,
            original_name=original_name,
            resolved_name=resolved_name,
            vmid=vmid,
            suffix_index=suffix_index,
            operator_renamed=operator_renamed,
        )
        await self._queue.put(("duplicate_name_resolved", msg))

    async def emit_hardware_discovery_progress(
        self,
        node: str,
        cluster: str | None = None,
        chassis_serial: str | None = None,
        chassis_manufacturer: str | None = None,
        chassis_product: str | None = None,
        nic_count: int = 0,
        duration_ms: int | None = None,
    ) -> None:
        """Emit a `hardware_discovery` progress frame for a single node."""
        msg = build_hardware_discovery_message(
            node=node,
            cluster=cluster,
            chassis_serial=chassis_serial,
            chassis_manufacturer=chassis_manufacturer,
            chassis_product=chassis_product,
            nic_count=nic_count,
            duration_ms=duration_ms,
        )
        await self._queue.put(("hardware_discovery", msg))

    # Legacy helper methods (preserved for compatibility)

    @staticmethod
    def _extract_row_id(payload: dict[str, object]) -> str | None:
        data = payload.get("data")
        if isinstance(data, dict):
            row_id = data.get("rowid") or data.get("name")
            if row_id not in (None, ""):
                return str(row_id)
        return None

    @staticmethod
    def _extract_status(payload: dict[str, object]) -> str:
        data = payload.get("data")
        if payload.get("end") is True:
            return "completed"
        if isinstance(data, dict):
            if data.get("error"):
                return "failed"
            if data.get("completed") is True:
                return "completed"
            return "progress"
        return "progress"

    @staticmethod
    def _build_message(
        object_name: str,
        payload: dict[str, object],
        row_id: str | None,
        status: str,
    ) -> str:
        if payload.get("end") is True:
            return f"{object_name} stream completed"
        data = payload.get("data")
        if isinstance(data, dict):
            warning = data.get("warning")
            if warning:
                return str(warning)
            error = data.get("error")
            if error:
                return str(error)
            if status == "completed":
                return f"Synced {object_name} {row_id or ''}".strip()
            if row_id:
                return f"Processing {object_name} {row_id}"
        return f"{object_name} {status}"


async def sse_stream_generator(  # noqa: C901
    bridge: WebSocketSSEBridge,
    sync_task: asyncio.Task,
    step_name: str,
    *,
    started_message: str | None = None,
    completed_message: str | None = None,
    failed_message: str | None = None,
    result_extractor: Callable[[object], dict[str, object]] | None = None,
    keepalive_interval: float = 15.0,
) -> AsyncIterator[str]:
    """Shared SSE generator for bridge-pattern streaming endpoints.

    Encapsulates try/except-CancelledError/except-Exception/finally in one place so
    every streaming endpoint gets consistent keepalive, clean CancelledError handling,
    and guaranteed task cleanup.  All three are required to prevent the 502 "stream
    ended without a complete event" error.

    Usage::

        async def event_stream():
            bridge = WebSocketSSEBridge()
            async def _run_sync():
                try:
                    return await my_sync_func(websocket=bridge, use_websocket=True)
                finally:
                    await bridge.close()
            sync_task = asyncio.create_task(_run_sync())
            async for frame in sse_stream_generator(bridge, sync_task, "my-stage"):
                yield frame

        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    Args:
        bridge: The ``WebSocketSSEBridge`` used by the sync function for progress events.
        sync_task: The asyncio.Task running the sync function.
        step_name: Stage identifier used in SSE event payloads (e.g. ``"virtual-machines"``).
        started_message: Optional override for the "started" event message.
        completed_message: Optional override for the "completed" and "complete" event messages.
        failed_message: Optional override for the "complete" event message on failure.
        result_extractor: Callable that receives the task return value and returns a dict for
            the SSE result payload.  Defaults to ``{"count": len(result)}``.
        keepalive_interval: Seconds between keepalive comments when the bridge queue is idle.
    """

    def _default_extract(result: object) -> dict[str, object]:
        if isinstance(result, list):
            return {"count": len(result)}
        if isinstance(result, dict):
            return result
        return {}

    extract = result_extractor or _default_extract

    try:
        yield sse_event(
            "step",
            {
                "step": step_name,
                "status": "started",
                "message": started_message or f"Starting {step_name} synchronization.",
            },
        )
        async for frame in bridge.iter_sse(keepalive_interval=keepalive_interval):
            yield frame

        result = await sync_task
        result_data = extract(result)
        yield sse_event(
            "step",
            {
                "step": step_name,
                "status": "completed",
                "message": completed_message or f"{step_name} synchronization finished.",
                "result": result_data,
            },
        )
        yield sse_event(
            "complete",
            {
                "ok": True,
                "message": completed_message or f"{step_name} sync completed.",
                "result": result_data,
            },
        )
    except asyncio.CancelledError:
        if not sync_task.done():
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass
        yield sse_event(
            "error",
            {
                "step": step_name,
                "status": "failed",
                "error": "Server shutdown or request cancelled.",
                "detail": "Server shutdown or request cancelled.",
            },
        )
        yield sse_event(
            "complete",
            {
                "ok": False,
                "message": f"{step_name} sync cancelled.",
                "errors": [{"detail": "Server shutdown or request cancelled."}],
            },
        )
    except Exception as error:  # noqa: BLE001
        if not sync_task.done():
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass
        yield sse_event(
            "error",
            {
                "step": step_name,
                "status": "failed",
                "error": str(error),
                "detail": str(error),
            },
        )
        yield sse_event(
            "complete",
            {
                "ok": False,
                "message": failed_message or f"{step_name} sync failed.",
                "errors": [{"detail": str(error)}],
            },
        )
    finally:
        if not sync_task.done():
            sync_task.cancel()
            try:
                await asyncio.shield(sync_task)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


# Standalone helper functions for use outside WebSocketSSEBridge context


def create_discovery_event(
    phase: str,
    items: list[dict[str, object]],
    message: str | None = None,
    metadata: dict[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    """Create a discovery event tuple for SSE emission.

    Returns:
        Tuple of (event_type, data) suitable for yield sse_event(*result)
    """
    return (
        "discovery",
        build_discovery_message(
            phase=phase,
            items=items,
            message=message,
            metadata=metadata,
        ),
    )


def create_substep_event(
    phase: str,
    substep: str,
    status: SubstepStatus | str,
    message: str,
    item: dict[str, object] | None = None,
    timing_ms: int | None = None,
    result: dict[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    """Create a substep event tuple for SSE emission."""
    status_val = status if isinstance(status, str) else status.value
    return (
        "substep",
        build_substep_message(
            phase=phase,
            substep=substep,
            status=status_val,
            message=message,
            item=item,
            timing_ms=timing_ms,
            result=result,
        ),
    )


def create_item_progress_event(
    phase: str,
    item: dict[str, object],
    operation: ItemOperation | str,
    status: str,
    message: str,
    progress_current: int,
    progress_total: int,
    timing_ms: int | None = None,
    error: str | None = None,
    warning: str | None = None,
) -> tuple[str, dict[str, object]]:
    """Create an item progress event tuple for SSE emission."""
    op_val = operation if isinstance(operation, str) else operation.value
    return (
        "item_progress",
        build_item_progress_message(
            phase=phase,
            item=item,
            operation=op_val,
            status=status,
            message=message,
            progress_current=progress_current,
            progress_total=progress_total,
            timing_ms=timing_ms,
            error=error,
            warning=warning,
        ),
    )


def create_phase_summary_event(
    phase: str,
    created: int = 0,
    updated: int = 0,
    deleted: int = 0,
    failed: int = 0,
    skipped: int = 0,
    timing_ms: int | None = None,
    message: str | None = None,
) -> tuple[str, dict[str, object]]:
    """Create a phase summary event tuple for SSE emission."""
    return (
        "phase_summary",
        build_phase_summary_message(
            phase=phase,
            created=created,
            updated=updated,
            deleted=deleted,
            failed=failed,
            skipped=skipped,
            timing_ms=timing_ms,
            message=message,
        ),
    )


def create_error_detail_event(
    message: str,
    category: ErrorCategory | str,
    phase: str | None = None,
    item: dict[str, object] | None = None,
    detail: str | None = None,
    suggestion: str | None = None,
    traceback: str | None = None,
) -> tuple[str, dict[str, object]]:
    """Create an error detail event tuple for SSE emission."""
    cat_val = category if isinstance(category, str) else category.value
    return (
        "error_detail",
        build_error_detail_message(
            message=message,
            category=cat_val,
            phase=phase,
            item=item,
            detail=detail,
            suggestion=suggestion,
            traceback=traceback,
        ),
    )


# Timing helper class for tracking operation durations


class TimingContext:
    """Context manager for timing operations."""

    def __init__(self, bridge: WebSocketSSEBridge, key: str) -> None:
        self.bridge = bridge
        self.key = key
        self.elapsed_ms: int | None = None

    def __enter__(self) -> "TimingContext":
        self.bridge.start_timer(self.key)
        return self

    def __exit__(self, *args) -> None:
        self.elapsed_ms = self.bridge.get_elapsed_ms(self.key)
        self.bridge.clear_timer(self.key)
