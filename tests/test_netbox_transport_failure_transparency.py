"""NetBox transport failures must never collapse to an empty-detail HTTP 400.

``str()`` of every asyncio/aiohttp timeout class is the empty string, so a
NetBox request that times out inside ``ensure_tag_async()`` used to reach the
plugin as ``{"message": "Error ensuring Proxbox tag", "detail": ""}`` with
HTTP 400 — and was never retried, because the transient classifier only
matched substrings of that empty text. These tests drive ``proxbox_tag()``
through the real ``netbox_rest`` stack with only the HTTP client patched.
"""

from __future__ import annotations

import aiohttp
import pytest

from proxbox_api.dependencies import proxbox_tag
from proxbox_api.exception import ProxboxException
from proxbox_api.utils import retry
from proxbox_api.utils.retry import (
    _is_connection_refused_error,
    _is_transient_netbox_error,
    describe_exception,
    is_netbox_connection_error,
    is_netbox_timeout_error,
)


class _FailingClient:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def request(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise self.error


class _FakeApi:
    def __init__(self, error: BaseException) -> None:
        self.client = _FailingClient(error)


def _no_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("proxbox_api.netbox_rest._resolve_netbox_max_retries", lambda: 0)
    monkeypatch.setattr("proxbox_api.netbox_rest._resolve_netbox_retry_delay", lambda: 0.0)


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ServerTimeoutError(),
        aiohttp.ConnectionTimeoutError(),
        TimeoutError(),
    ],
    ids=["server-timeout", "connection-timeout", "builtin-timeout"],
)
def test_timeout_exceptions_stringify_empty(error: BaseException) -> None:
    """The premise: without a class-name fallback there is nothing to show."""
    assert str(error) == ""
    assert describe_exception(error) == type(error).__name__


@pytest.mark.parametrize("configured", ["37", "7.5"], ids=["int", "float"])
def test_error_path_timeout_resolver_matches_session_resolver(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    """The error text must name the same timeout the NetBox client actually uses."""
    from proxbox_api.session.netbox import _resolve_netbox_timeout

    monkeypatch.setenv("PROXBOX_NETBOX_TIMEOUT", configured)
    assert retry._resolve_configured_netbox_timeout() == _resolve_netbox_timeout()
    assert retry._resolve_configured_netbox_timeout() == float(configured)


def test_error_path_timeout_resolver_default_matches_session_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proxbox_api.session import netbox as session_netbox

    monkeypatch.delenv("PROXBOX_NETBOX_TIMEOUT", raising=False)
    assert retry._DEFAULT_NETBOX_TIMEOUT_SECONDS == session_netbox._DEFAULT_NETBOX_TIMEOUT
    assert retry._resolve_configured_netbox_timeout() == session_netbox._resolve_netbox_timeout()


def test_describe_exception_prefixes_class_name_once() -> None:
    assert describe_exception(RuntimeError("boom")) == "RuntimeError: boom"
    already = RuntimeError("RuntimeError: boom")
    assert describe_exception(already) == "RuntimeError: boom"


@pytest.mark.parametrize(
    "error",
    [aiohttp.ServerTimeoutError(), aiohttp.ConnectionTimeoutError(), TimeoutError()],
    ids=["server-timeout", "connection-timeout", "builtin-timeout"],
)
def test_timeouts_are_transient_by_type(error: BaseException) -> None:
    assert is_netbox_timeout_error(error)
    assert _is_transient_netbox_error(error)


def test_connection_errors_are_transient_by_type() -> None:
    error = aiohttp.ClientConnectorError(
        aiohttp.client_reqrep.ConnectionKey("netbox", 8000, False, None, None, None, None),
        OSError(111, "Connect call failed"),
    )
    assert is_netbox_connection_error(error)
    assert _is_transient_netbox_error(error)
    assert _is_connection_refused_error(error)


def test_plain_value_error_is_not_transient() -> None:
    error = ValueError("Invalid slug")
    assert not is_netbox_timeout_error(error)
    assert not is_netbox_connection_error(error)
    assert not _is_transient_netbox_error(error)


@pytest.mark.asyncio
async def test_proxbox_tag_timeout_surfaces_cause_and_504(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_retries(monkeypatch)
    # ``netbox_rest`` binds the resolver by name at import time, so patch it there;
    # a non-default sentinel proves the detail reports the *effective* timeout.
    monkeypatch.setattr("proxbox_api.netbox_rest._resolve_configured_netbox_timeout", lambda: 37.0)
    api = _FakeApi(aiohttp.ServerTimeoutError())

    with pytest.raises(ProxboxException) as exc_info:
        await proxbox_tag(api)

    raised = exc_info.value
    assert raised.message == "Error ensuring Proxbox tag"
    assert raised.http_status_code == 504
    detail = str(raised.detail)
    assert detail.strip(), "detail must never be empty"
    assert "ServerTimeoutError" in detail
    assert "37s" in detail
    assert "120" not in detail
    assert "PROXBOX_NETBOX_TIMEOUT" in detail
    assert "reachable" in detail
    assert "/api/extras/tags/" in detail


@pytest.mark.asyncio
async def test_proxbox_tag_connection_refused_surfaces_cause_and_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_retries(monkeypatch)
    error = aiohttp.ClientConnectorError(
        aiohttp.client_reqrep.ConnectionKey("netbox", 8000, False, None, None, None, None),
        OSError(111, "Connect call failed"),
    )
    api = _FakeApi(error)

    with pytest.raises(ProxboxException) as exc_info:
        await proxbox_tag(api)

    raised = exc_info.value
    assert raised.message == "Error ensuring Proxbox tag"
    assert raised.http_status_code == 502
    detail = str(raised.detail)
    assert "ClientConnectorError" in detail
    assert "netbox:8000" in detail
    assert "reachable" in detail


def _connector_error() -> aiohttp.ClientConnectorError:
    return aiohttp.ClientConnectorError(
        aiohttp.client_reqrep.ConnectionKey("netbox", 8000, False, None, None, None, None),
        OSError(111, "Connect call failed"),
    )


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ServerTimeoutError(),
        _connector_error(),
        # A mid-connection reset: its text matches no substring indicator, so only
        # type-based classification through the ProxboxException wrapper retries it.
        aiohttp.ClientOSError(104, "Connection reset by peer"),
    ],
    ids=["timeout", "connect-refused", "reset-by-peer"],
)
@pytest.mark.asyncio
async def test_transport_failures_are_retried_before_failing(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    monkeypatch.setattr("proxbox_api.netbox_rest._resolve_netbox_max_retries", lambda: 2)
    monkeypatch.setattr("proxbox_api.netbox_rest._resolve_netbox_retry_delay", lambda: 0.0)
    monkeypatch.setattr("proxbox_api.netbox_rest._compute_retry_delay", lambda *a, **k: 0.0)
    api = _FakeApi(error)

    with pytest.raises(ProxboxException) as exc_info:
        await proxbox_tag(api)

    assert api.client.calls == 3
    assert exc_info.value.http_status_code in (502, 504)


def _wrapped(cause: BaseException) -> ProxboxException:
    try:
        raise ProxboxException("NetBox create failed", detail="wrapped") from cause
    except ProxboxException as wrapped:
        return wrapped


def test_wrapped_transport_failure_is_still_transient() -> None:
    """The REST retry loop sees the ProxboxException wrapper, not the aiohttp error."""
    reset = _wrapped(aiohttp.ClientOSError(104, "Connection reset by peer"))
    assert _is_transient_netbox_error(reset)
    # A reset may follow a committed write: never "connection refused", so a
    # lookup-free POST is not retried on it.
    assert not _is_connection_refused_error(reset)
    disconnected = _wrapped(aiohttp.ServerDisconnectedError())
    assert _is_transient_netbox_error(disconnected)
    assert not _is_connection_refused_error(disconnected)
    refused = _wrapped(_connector_error())
    assert _is_transient_netbox_error(refused)
    assert _is_connection_refused_error(refused)
    assert not _is_transient_netbox_error(ProxboxException("NetBox list failed"))


class _CountingClient:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.posts = 0

    async def request(self, method: str, *args: object, **kwargs: object) -> object:
        if method == "POST":
            self.posts += 1
        raise self.error


@pytest.mark.parametrize(
    ("error", "expected_posts"),
    [
        (aiohttp.ClientOSError(104, "Connection reset by peer"), 1),
        (aiohttp.ServerDisconnectedError(), 1),
        (_connector_error(), 3),
    ],
    ids=["reset-once", "disconnect-once", "refused-retried"],
)
@pytest.mark.asyncio
async def test_lookup_free_create_retries_only_when_never_connected(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, expected_posts: int
) -> None:
    """A POST without a lookup is re-sent only when NetBox provably never got it."""
    from proxbox_api.netbox_rest import rest_create_async

    monkeypatch.setattr("proxbox_api.netbox_rest._resolve_netbox_max_retries", lambda: 2)
    monkeypatch.setattr("proxbox_api.netbox_rest._resolve_netbox_retry_delay", lambda: 0.0)
    monkeypatch.setattr("proxbox_api.netbox_rest._compute_retry_delay", lambda *a, **k: 0.0)
    api = _FakeApi(error)
    api.client = _CountingClient(error)

    with pytest.raises(ProxboxException):
        await rest_create_async(api, "/api/extras/tags/", {"name": "x", "slug": "x"})

    assert api.client.posts == expected_posts


def test_exception_chain_is_bounded_and_cycle_safe() -> None:
    from proxbox_api.utils.retry import _exception_chain

    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert _exception_chain(a) == [a, b]
    head = RuntimeError("0")
    node = head
    for i in range(1, 20):
        nxt = RuntimeError(str(i))
        node.__cause__ = nxt
        node = nxt
    assert len(_exception_chain(head)) == 8


@pytest.mark.asyncio
async def test_proxbox_tag_never_emits_empty_detail() -> None:
    """A ProxboxException with no detail still reaches the plugin with a cause."""
    from unittest.mock import AsyncMock, patch

    upstream = ProxboxException("NetBox list failed", detail="", python_exception="")
    with patch(
        "proxbox_api.dependencies.ensure_tag_async",
        new_callable=AsyncMock,
        side_effect=upstream,
    ):
        with pytest.raises(ProxboxException) as exc_info:
            await proxbox_tag(object())

    raised = exc_info.value
    assert raised.message == "Error ensuring Proxbox tag"
    assert isinstance(raised.detail, str) and raised.detail.strip()
    assert "NetBox list failed" in raised.detail
