"""Tests for detailed live-stream message contracts."""

from __future__ import annotations

import json

import pytest

from proxbox_api.schemas.stream_messages import ErrorCategory, ItemOperation, SubstepStatus
from proxbox_api.utils.streaming import WebSocketSSEBridge


def _parse_sse_frame(frame: str) -> tuple[str, dict[str, object]]:
    lines = [line for line in frame.strip().splitlines() if line]
    event = lines[0].replace("event: ", "", 1)
    data = json.loads(lines[1].replace("data: ", "", 1))
    return event, data


@pytest.mark.asyncio
async def test_bridge_emits_detailed_event_types() -> None:
    bridge = WebSocketSSEBridge()

    await bridge.emit_discovery(
        phase="devices",
        items=[{"name": "pve01", "type": "node"}],
        message="Discovered 1 device",
    )
    await bridge.emit_substep(
        phase="devices",
        substep="ensure_cluster",
        status=SubstepStatus.PROCESSING,
        message="Ensuring cluster",
        item={"name": "pve01"},
    )
    await bridge.emit_item_progress(
        phase="devices",
        item={"name": "pve01", "type": "node"},
        operation=ItemOperation.CREATED,
        status="completed",
        message="Synced device pve01",
        progress_current=1,
        progress_total=1,
    )
    await bridge.emit_phase_summary(
        phase="devices",
        created=1,
        failed=0,
        message="Device sync complete",
    )
    await bridge.emit_error_detail(
        message="Validation error",
        category=ErrorCategory.VALIDATION,
        phase="devices",
        item={"name": "pve01"},
        suggestion="Check device role",
    )
    await bridge.close()

    frames = []
    async for frame in bridge.iter_sse():
        frames.append(_parse_sse_frame(frame))

    event_names = [event for event, _ in frames]
    assert event_names == [
        "discovery",
        "substep",
        "item_progress",
        "phase_summary",
        "error_detail",
    ]

    discovery_payload = frames[0][1]
    assert discovery_payload["phase"] == "devices"
    assert discovery_payload["count"] == 1
    assert discovery_payload["items"][0]["name"] == "pve01"

    substep_payload = frames[1][1]
    assert substep_payload["substep"] == "ensure_cluster"
    assert substep_payload["status"] == "processing"

    item_payload = frames[2][1]
    assert item_payload["operation"] == "created"
    assert item_payload["progress"]["current"] == 1
    assert item_payload["progress"]["total"] == 1

    summary_payload = frames[3][1]
    assert summary_payload["result"]["created"] == 1
    assert summary_payload["result"]["failed"] == 0

    error_payload = frames[4][1]
    assert error_payload["category"] == "validation"
    assert error_payload["suggestion"] == "Check device role"


@pytest.mark.asyncio
async def test_bridge_legacy_send_json_still_emits_step_event() -> None:
    bridge = WebSocketSSEBridge()
    await bridge.send_json(
        {
            "object": "device",
            "type": "create",
            "data": {"rowid": "pve01", "completed": False},
        }
    )
    await bridge.close()

    frames = []
    async for frame in bridge.iter_sse():
        frames.append(_parse_sse_frame(frame))

    assert len(frames) == 1
    assert frames[0][0] == "step"
    assert frames[0][1]["step"] == "device"
    assert frames[0][1]["status"] == "progress"
