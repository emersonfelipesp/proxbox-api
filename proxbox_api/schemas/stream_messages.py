"""Structured streaming message schemas for live job updates.

This module defines the message types and schemas used forServer-Sent Events
and WebSocket communication during synchronization jobs. Messages provide
detailed, real-time feedback about sync progress, discoveries, sub-operations,
phase summaries, and error details.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field

from proxbox_api.schemas._base import ProxboxBaseModel


class StreamMessageType(str, Enum):
    """Types of streaming messages for job updates."""

    DISCOVERY = "discovery"
    SUBSTEP = "substep"
    ITEM_PROGRESS = "item_progress"
    PHASE_SUMMARY = "phase_summary"
    ERROR_DETAIL = "error_detail"
    PROGRESS = "progress"
    DUPLICATE_NAME_RESOLVED = "duplicate_name_resolved"
    # Migrate verb (operational-verbs.md §7.1) — dedicated SSE channel.
    # The migrate route emits these names verbatim; mirrored by the
    # contract at contracts/proxbox_api_sse_schema.json on the
    # netbox-proxbox side.
    MIGRATE_DISPATCHED = "migrate_dispatched"
    MIGRATE_PROGRESS = "migrate_progress"
    MIGRATE_SUCCEEDED = "migrate_succeeded"
    MIGRATE_FAILED = "migrate_failed"


class SubstepStatus(str, Enum):
    """Status values for substep messages."""

    STARTED = "started"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ItemOperation(str, Enum):
    """Operation types for item processing."""

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    WOULD_DELETE = "would_delete"
    SKIPPED = "skipped"
    FAILED = "failed"


class ErrorCategory(str, Enum):
    """Categories for structured error reporting."""

    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class ProgressInfo(ProxboxBaseModel):
    """Progress tracking information."""

    current: int = Field(default=0, description="Current item count")
    total: int = Field(default=0, description="Total item count")
    percent: float = Field(default=0.0, description="Percentage complete (0-100)")


class TimingInfo(ProxboxBaseModel):
    """Timing information for operations."""

    elapsed_ms: int | None = Field(default=None, description="Elapsed time in milliseconds")
    started_at: str | None = Field(default=None, description="ISO timestamp when started")
    finished_at: str | None = Field(default=None, description="ISO timestamp when finished")


class ItemInfo(ProxboxBaseModel):
    """Information about an item being processed."""

    name: str = Field(description="Item name/identifier")
    netbox_id: int | None = Field(default=None, description="NetBox object ID if created")
    netbox_url: str | None = Field(default=None, description="NetBox URL if created")
    item_type: str | None = Field(default=None, description="Item type (node, vm, etc.)")
    extra: dict[str, Any] | None = Field(default=None, description="Additional item metadata")


class DiscoveryMessage(ProxboxBaseModel):
    """Message sent before processing begins, listing items to be synced."""

    event: str = Field(default="discovery", description="Message event type")
    phase: str = Field(description="Phase name (devices, virtual-machines, etc.)")
    status: str = Field(default="discovered", description="Discovery status")
    message: str = Field(description="Human-readable message")
    count: int = Field(description="Number of items discovered")
    items: list[ItemInfo] = Field(default_factory=list, description="List of discovered items")
    progress: ProgressInfo | None = Field(default=None, description="Initial progress state")
    metadata: dict[str, Any] | None = Field(default=None, description="Additional phase metadata")


class SubstepMessage(ProxboxBaseModel):
    """Message for granular sub-operations within an item sync."""

    event: str = Field(default="substep", description="Message event type")
    phase: str = Field(description="Phase name")
    substep: str = Field(description="Substep identifier (e.g., ensure_cluster, create_device)")
    status: SubstepStatus = Field(description="Substep status")
    message: str = Field(description="Human-readable message")
    item: ItemInfo | None = Field(default=None, description="Item being processed")
    timing: TimingInfo | None = Field(default=None, description="Timing information")
    result: dict[str, Any] | None = Field(default=None, description="Substep result data")


class ItemProgressMessage(ProxboxBaseModel):
    """Message for item-level progress updates."""

    event: str = Field(default="item_progress", description="Message event type")
    phase: str = Field(description="Phase name")
    status: str = Field(description="Item status (processing, completed, failed)")
    message: str = Field(description="Human-readable message")
    item: ItemInfo = Field(description="Item being processed")
    operation: ItemOperation = Field(description="Operation performed")
    progress: ProgressInfo = Field(description="Progress state")
    timing: TimingInfo | None = Field(default=None, description="Timing information")
    error: str | None = Field(default=None, description="Error message if failed")
    warning: str | None = Field(default=None, description="Warning message if applicable")


class PhaseSummaryMessage(ProxboxBaseModel):
    """Summary message sent at the end of a sync phase."""

    event: str = Field(default="phase_summary", description="Message event type")
    phase: str = Field(description="Phase name")
    status: str = Field(default="completed", description="Phase status")
    message: str = Field(description="Human-readable summary message")
    result: dict[str, int] = Field(description="Result counts (created, updated, failed, skipped)")
    timing: TimingInfo | None = Field(default=None, description="Phase timing information")


class ErrorDetailMessage(ProxboxBaseModel):
    """Detailed error message with categorization and remediation hints."""

    event: str = Field(default="error_detail", description="Message event type")
    phase: str | None = Field(default=None, description="Phase where error occurred")
    item: ItemInfo | None = Field(default=None, description="Item associated with error")
    category: ErrorCategory = Field(description="Error category")
    message: str = Field(description="Human-readable error message")
    detail: str | None = Field(default=None, description="Technical error details")
    suggestion: str | None = Field(default=None, description="Suggested remediation")
    traceback: str | None = Field(default=None, description="Stack trace (debug mode only)")


class DuplicateNameResolvedMessage(ProxboxBaseModel):
    """Warning frame emitted when the name-collision resolver renames a VM.

    A frame is emitted in two cases: (1) the resolver applied an algorithmic
    " (N)" suffix because the candidate name was already taken in the target
    NetBox cluster, or (2) the existing NetBox record was operator-renamed and
    the sync skipped the rename. Consumers can surface a warning UI without
    failing the sync.
    """

    event: str = Field(default="duplicate_name_resolved", description="Message event type")
    cluster: str = Field(description="Proxmox cluster name (human label)")
    original_name: str = Field(description="Candidate VM name from Proxmox")
    resolved_name: str = Field(description="Final name written to NetBox")
    vmid: int = Field(description="Proxmox VMID")
    suffix_index: int = Field(
        description="1 = no algorithmic suffix (operator-rename flow); 2+ = suffix applied",
    )
    operator_renamed: bool = Field(
        default=False,
        description="True when the NetBox record was already manually renamed by an operator",
    )


class StreamMessage(ProxboxBaseModel):
    """Union type for all stream messages."""

    event: StreamMessageType = Field(description="Message event type")
    data: dict[str, Any] = Field(description="Message payload")


# Builder functions for creating messages


def build_discovery_message(
    phase: str,
    items: list[dict[str, Any]],
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a discovery message for the start of a phase."""
    count = len(items)
    item_infos = [
        ItemInfo(
            name=item.get("name", str(item.get("id", "unknown"))),
            netbox_id=item.get("netbox_id"),
            netbox_url=item.get("netbox_url"),
            item_type=item.get("type"),
            extra=item.get("extra"),
        )
        for item in items
    ]
    msg = DiscoveryMessage(
        phase=phase,
        message=message or f"Discovered {count} {phase} to process",
        count=count,
        items=item_infos,
        progress=ProgressInfo(current=0, total=count, percent=0.0),
        metadata=metadata,
    )
    return msg.model_dump(exclude_none=True)


def build_substep_message(
    phase: str,
    substep: str,
    status: SubstepStatus,
    message: str,
    item: dict[str, Any] | None = None,
    timing_ms: int | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a substep message for granular operations."""
    item_info = None
    if item:
        item_info = ItemInfo(
            name=item.get("name", str(item.get("id", "unknown"))),
            netbox_id=item.get("netbox_id"),
            netbox_url=item.get("netbox_url"),
            item_type=item.get("type"),
            extra=item.get("extra"),
        )
    timing = None
    if timing_ms is not None:
        timing = TimingInfo(elapsed_ms=timing_ms)
    msg = SubstepMessage(
        phase=phase,
        substep=substep,
        status=status,
        message=message,
        item=item_info,
        timing=timing,
        result=result,
    )
    return msg.model_dump(exclude_none=True)


def build_item_progress_message(
    phase: str,
    item: dict[str, Any],
    operation: ItemOperation,
    status: str,
    message: str,
    progress_current: int,
    progress_total: int,
    timing_ms: int | None = None,
    error: str | None = None,
    warning: str | None = None,
) -> dict[str, Any]:
    """Build an item progress message."""
    item_info = ItemInfo(
        name=item.get("name", str(item.get("id", "unknown"))),
        netbox_id=item.get("netbox_id"),
        netbox_url=item.get("netbox_url"),
        item_type=item.get("type"),
        extra=item.get("extra"),
    )
    percent = (progress_current / progress_total * 100) if progress_total > 0 else 0.0
    timing = None
    if timing_ms is not None:
        timing = TimingInfo(elapsed_ms=timing_ms)
    msg = ItemProgressMessage(
        phase=phase,
        status=status,
        message=message,
        item=item_info,
        operation=operation,
        progress=ProgressInfo(current=progress_current, total=progress_total, percent=percent),
        timing=timing,
        error=error,
        warning=warning,
    )
    return msg.model_dump(exclude_none=True)


def build_phase_summary_message(
    phase: str,
    created: int,
    updated: int,
    deleted: int,
    failed: int,
    skipped: int,
    timing_ms: int | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Build a phase summary message."""
    total = created + updated + deleted + failed + skipped
    timing = None
    if timing_ms is not None:
        timing = TimingInfo(elapsed_ms=timing_ms)
    status = "completed" if failed == 0 else "completed_with_errors"
    msg = PhaseSummaryMessage(
        phase=phase,
        status=status,
        message=message
        or f"Phase {phase} completed: {created} created, {updated} updated, {failed} failed",
        result={
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "failed": failed,
            "skipped": skipped,
            "total": total,
        },
        timing=timing,
    )
    return msg.model_dump(exclude_none=True)


def build_error_detail_message(
    message: str,
    category: ErrorCategory,
    phase: str | None = None,
    item: dict[str, Any] | None = None,
    detail: str | None = None,
    suggestion: str | None = None,
    traceback: str | None = None,
) -> dict[str, Any]:
    """Build a detailed error message."""
    item_info = None
    if item:
        item_info = ItemInfo(
            name=item.get("name", str(item.get("id", "unknown"))),
            netbox_id=item.get("netbox_id"),
            netbox_url=item.get("netbox_url"),
            item_type=item.get("type"),
            extra=item.get("extra"),
        )
    msg = ErrorDetailMessage(
        phase=phase,
        item=item_info,
        category=category,
        message=message,
        detail=detail,
        suggestion=suggestion,
        traceback=traceback,
    )
    return msg.model_dump(exclude_none=True)


def build_duplicate_name_resolved_message(
    cluster: str,
    original_name: str,
    resolved_name: str,
    vmid: int,
    suffix_index: int,
    operator_renamed: bool = False,
) -> dict[str, Any]:
    """Build a `duplicate_name_resolved` warning frame."""
    msg = DuplicateNameResolvedMessage(
        cluster=cluster,
        original_name=original_name,
        resolved_name=resolved_name,
        vmid=vmid,
        suffix_index=suffix_index,
        operator_renamed=operator_renamed,
    )
    return msg.model_dump(exclude_none=True)


# Helper to format elapsed time
def format_timing_ms(elapsed_ms: int) -> str:
    """Format elapsed milliseconds into human-readable string."""
    if elapsed_ms < 1000:
        return f"{elapsed_ms}ms"
    seconds = elapsed_ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    return f"{hours:.1f}h"
