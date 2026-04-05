"""Run async coroutines from synchronous code via a persistent background loop.

Using ``asyncio.run`` repeatedly for the same async client object can close the loop
that owns internal resources (for example ``aiohttp`` sessions), which later triggers
``Event loop is closed``. This module keeps one daemon thread + loop alive for all
sync-bridged coroutine executions.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from concurrent.futures import Future

_loop_lock = threading.Lock()
_loop_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _ensure_background_loop() -> asyncio.AbstractEventLoop:
    """Create (once) and return a long-lived background event loop."""
    global _loop_thread, _loop

    with _loop_lock:
        if _loop is not None and _loop.is_running() and _loop_thread and _loop_thread.is_alive():
            return _loop

        ready = threading.Event()

        def _runner() -> None:
            global _loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _loop = loop
            ready.set()
            loop.run_forever()

        _loop_thread = threading.Thread(target=_runner, daemon=True, name="proxbox-async-bridge")
        _loop_thread.start()
        ready.wait()
        if _loop is None:
            raise RuntimeError("Failed to initialize background async loop")
        return _loop


def run_coroutine_blocking(coro: Awaitable[object]) -> object:
    """Execute ``coro`` to completion and return its result."""
    loop = _ensure_background_loop()
    future: Future[object] = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()
