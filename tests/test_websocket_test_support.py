from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import contextmanager

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.websocket_test_support import rejected_websocket_session, websocket_session


class _Client:
    def __init__(self, session):
        self.session = session

    def websocket_connect(self, *args, **kwargs):
        return self.session


@contextmanager
def _teardown_cancelled_session():
    yield object()
    raise FutureCancelledError


@contextmanager
def _entry_cancelled_session():
    raise FutureCancelledError
    yield


@contextmanager
def _rejected_session(code: int):
    raise WebSocketDisconnect(code=code)
    yield


@contextmanager
def _accepted_session():
    yield object()


def test_websocket_session_suppresses_cancellation_only_after_completed_body() -> None:
    with websocket_session(_Client(_teardown_cancelled_session()), "/ws"):
        pass


def test_websocket_session_preserves_cancellation_from_entry() -> None:
    with pytest.raises(FutureCancelledError):
        with websocket_session(_Client(_entry_cancelled_session()), "/ws"):
            pass


def test_websocket_session_preserves_cancellation_from_body() -> None:
    with pytest.raises(FutureCancelledError):
        with websocket_session(_Client(_teardown_cancelled_session()), "/ws"):
            raise FutureCancelledError


def test_rejected_websocket_session_accepts_expected_disconnect() -> None:
    with rejected_websocket_session(_Client(_rejected_session(1008)), "/ws", expected_code=1008):
        pass


def test_rejected_websocket_session_accepts_masked_entry_cleanup_cancellation() -> None:
    with rejected_websocket_session(_Client(_entry_cancelled_session()), "/ws", expected_code=1008):
        pass


def test_rejected_websocket_session_preserves_wrong_disconnect_code() -> None:
    with pytest.raises(WebSocketDisconnect):
        with rejected_websocket_session(
            _Client(_rejected_session(1002)), "/ws", expected_code=1008
        ):
            pass


def test_rejected_websocket_session_rejects_successful_handshake() -> None:
    with pytest.raises(AssertionError, match="unexpectedly succeeded"):
        with rejected_websocket_session(_Client(_accepted_session()), "/ws", expected_code=1008):
            pass
