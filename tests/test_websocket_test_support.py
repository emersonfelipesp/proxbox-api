from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import contextmanager

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.websocket_test_support import (
    rejected_websocket_session,
    websocket_error_after_effect,
    websocket_session,
)


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


@contextmanager
def _other_error_session():
    raise RuntimeError("unexpected failure")
    yield


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


def test_websocket_error_after_effect_accepts_expected_error() -> None:
    observed = True
    with websocket_error_after_effect(
        _Client(_rejected_session(1008)),
        "/ws",
        expected_error=WebSocketDisconnect,
        effect_observed=lambda: observed,
    ):
        pass


def test_websocket_error_after_effect_accepts_masked_cleanup_after_effect() -> None:
    observed = True
    with websocket_error_after_effect(
        _Client(_entry_cancelled_session()),
        "/ws",
        expected_error=WebSocketDisconnect,
        effect_observed=lambda: observed,
    ):
        pass


def test_websocket_error_after_effect_preserves_entry_cancellation_without_effect() -> None:
    with pytest.raises(FutureCancelledError):
        with websocket_error_after_effect(
            _Client(_entry_cancelled_session()),
            "/ws",
            expected_error=WebSocketDisconnect,
            effect_observed=lambda: False,
        ):
            pass


def test_websocket_error_after_effect_rejects_missing_effect() -> None:
    with pytest.raises(AssertionError, match="effect was not observed"):
        with websocket_error_after_effect(
            _Client(_rejected_session(1008)),
            "/ws",
            expected_error=WebSocketDisconnect,
            effect_observed=lambda: False,
        ):
            pass


def test_websocket_error_after_effect_rejects_successful_handshake() -> None:
    with pytest.raises(AssertionError, match="application error was not raised"):
        with websocket_error_after_effect(
            _Client(_accepted_session()),
            "/ws",
            expected_error=WebSocketDisconnect,
            effect_observed=lambda: True,
        ):
            pass


@pytest.mark.parametrize("broad_error", [AssertionError, Exception, BaseException])
def test_websocket_error_after_effect_broad_error_cannot_hide_success(broad_error) -> None:
    with pytest.raises(AssertionError, match="application error was not raised"):
        with websocket_error_after_effect(
            _Client(_accepted_session()),
            "/ws",
            expected_error=broad_error,
            effect_observed=lambda: True,
        ):
            pass


def test_websocket_error_after_effect_broad_error_cannot_hide_entry_cancellation() -> None:
    with pytest.raises(FutureCancelledError):
        with websocket_error_after_effect(
            _Client(_entry_cancelled_session()),
            "/ws",
            expected_error=BaseException,
            effect_observed=lambda: False,
        ):
            pass


def test_websocket_error_after_effect_preserves_other_errors() -> None:
    with pytest.raises(RuntimeError, match="unexpected failure"):
        with websocket_error_after_effect(
            _Client(_other_error_session()),
            "/ws",
            expected_error=WebSocketDisconnect,
            effect_observed=lambda: True,
        ):
            pass
