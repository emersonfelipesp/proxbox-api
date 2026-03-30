"""Run asyncio coroutines from synchronous code when an event loop may already be running."""

from __future__ import annotations

import asyncio
import threading
from typing import Any


def run_coroutine_blocking(coro: Any) -> Any:
    """Execute ``coro`` to completion and return its result.

    If called from a thread with a running loop, runs the coroutine in a
    dedicated thread with its own ``asyncio.run`` (same behavior as the
    previous duplicated helpers in ``netbox_rest`` and ``netbox_sdk_sync``).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {"value": None, "error": None}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as error:  # noqa: BLE001
            result["error"] = error

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if result["error"] is not None:
        raise result["error"]
    return result["value"]
