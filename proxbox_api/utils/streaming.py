"""Helpers for server-sent event streaming in sync endpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any


def sse_event(event: str, data: Any) -> str:
    """Serialize one SSE frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class WebSocketSSEBridge:
    """Compatibility bridge that turns websocket-like JSON payloads into SSE frames."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    async def send_json(self, payload: dict[str, Any]) -> None:
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

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        await self._queue.put((event, data))

    async def close(self) -> None:
        await self._queue.put(None)

    async def iter_sse(self) -> AsyncIterator[str]:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            event, data = item
            yield sse_event(event, data)

    @staticmethod
    def _extract_row_id(payload: dict[str, Any]) -> str | None:
        data = payload.get("data")
        if isinstance(data, dict):
            row_id = data.get("rowid") or data.get("name")
            if row_id not in (None, ""):
                return str(row_id)
        return None

    @staticmethod
    def _extract_status(payload: dict[str, Any]) -> str:
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
        payload: dict[str, Any],
        row_id: str | None,
        status: str,
    ) -> str:
        if payload.get("end") is True:
            return f"{object_name} stream completed"
        data = payload.get("data")
        if isinstance(data, dict):
            error = data.get("error")
            if error:
                return str(error)
            if status == "completed":
                return f"Synced {object_name} {row_id or ''}".strip()
            if row_id:
                return f"Processing {object_name} {row_id}"
        return f"{object_name} {status}"
