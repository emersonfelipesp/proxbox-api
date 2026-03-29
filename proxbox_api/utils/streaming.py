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
        status = "completed" if payload.get("end") is True else "progress"
        await self.emit(
            "step",
            {
                "step": object_name,
                "status": status,
                "message": f"{object_name} {status}",
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
