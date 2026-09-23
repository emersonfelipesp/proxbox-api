"""Narrow helpers for deterministic Starlette WebSocket test teardown."""

from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import contextmanager
from typing import Any

from starlette.websockets import WebSocketDisconnect


@contextmanager
def websocket_session(client: Any, *args: Any, **kwargs: Any) -> Iterator[Any]:
    """Suppress only a cancellation raised after the WebSocket body completed."""
    body_completed = False
    try:
        with client.websocket_connect(*args, **kwargs) as websocket:
            yield websocket
            body_completed = True
    except FutureCancelledError:
        if not body_completed:
            raise


@contextmanager
def rejected_websocket_session(
    client: Any,
    *args: Any,
    expected_code: int,
    **kwargs: Any,
) -> Iterator[None]:
    """Accept only an expected handshake rejection or its masked cleanup race."""
    try:
        with client.websocket_connect(*args, **kwargs):
            raise AssertionError("WebSocket handshake unexpectedly succeeded")
    except WebSocketDisconnect as exc:
        if exc.code != expected_code:
            raise
    except FutureCancelledError:
        # Starlette can replace the already-raised handshake rejection while
        # unwinding its portal future. This helper is restricted to callers
        # that independently assert the server-side rejection contract.
        pass
    yield


@contextmanager
def websocket_error_after_effect(
    client: Any,
    *args: Any,
    expected_error: type[BaseException],
    effect_observed: Callable[[], bool],
    **kwargs: Any,
) -> Iterator[None]:
    """Accept an application error even when Starlette masks it during cleanup."""
    try:
        with client.websocket_connect(*args, **kwargs):
            pass
    except FutureCancelledError:
        if not effect_observed():
            raise
    except expected_error:
        pass
    else:
        raise AssertionError("WebSocket application error was not raised")
    if not effect_observed():
        raise AssertionError("WebSocket application effect was not observed")
    yield
