"""Tests for proxbox_tag() dependency error transparency.

The `proxbox_tag` FastAPI dependency gates the DCIM/devices sync stage. When
ensuring the Proxbox tag fails it must NOT flatten every underlying cause to a
bare HTTP 400 "Error ensuring Proxbox tag" — a real NetBox 5xx (or any error
carrying its own status) has to surface with that status and its detail so the
plugin logs the true cause instead of an opaque 400.
"""

from __future__ import annotations

import json as _json
from unittest.mock import AsyncMock, patch

import pytest

from proxbox_api.dependencies import proxbox_tag
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import _extract_payload


class _FakeResponse:
    """Minimal ApiResponse stand-in for _extract_payload."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.text = body

    def json(self) -> object:
        return _json.loads(self.text)


def test_extract_payload_surfaces_upstream_5xx_status() -> None:
    """A real NetBox 5xx must propagate its status, not flatten to 400."""
    resp = _FakeResponse(503, _json.dumps({"detail": "Service Unavailable"}))
    with pytest.raises(ProxboxException) as exc_info:
        _extract_payload(resp)
    assert exc_info.value.http_status_code == 503
    assert "Service Unavailable" in str(exc_info.value.detail)


def test_extract_payload_keeps_default_400_for_client_4xx() -> None:
    """A NetBox 4xx keeps the class default so validation/duplicate flows are unchanged."""
    resp = _FakeResponse(400, _json.dumps({"detail": "Invalid"}))
    with pytest.raises(ProxboxException) as exc_info:
        _extract_payload(resp)
    assert exc_info.value.http_status_code == 400


@pytest.mark.asyncio
async def test_proxbox_tag_preserves_upstream_status_and_detail() -> None:
    """A ProxboxException carrying a 502 must surface as 502, not a flat 400."""
    upstream = ProxboxException(
        "NetBox returned an error",
        detail={"error": "Bad Gateway talking to NetBox"},
        http_status_code=502,
    )

    with patch(
        "proxbox_api.dependencies.ensure_tag_async",
        new_callable=AsyncMock,
        side_effect=upstream,
    ):
        with pytest.raises(ProxboxException) as exc_info:
            await proxbox_tag(object())

    raised = exc_info.value
    assert raised.message == "Error ensuring Proxbox tag"
    # The real status is preserved, not downgraded to the class default 400.
    assert raised.http_status_code == 502
    # The underlying detail is carried through for diagnosis.
    assert raised.detail == {"error": "Bad Gateway talking to NetBox"}
    assert raised.__cause__ is upstream


@pytest.mark.asyncio
async def test_proxbox_tag_defaults_to_400_but_exposes_generic_cause() -> None:
    """A non-Proxbox error has no reliable status, but its cause is exposed."""
    boom = RuntimeError("connection refused to netbox")

    with patch(
        "proxbox_api.dependencies.ensure_tag_async",
        new_callable=AsyncMock,
        side_effect=boom,
    ):
        with pytest.raises(ProxboxException) as exc_info:
            await proxbox_tag(object())

    raised = exc_info.value
    assert raised.message == "Error ensuring Proxbox tag"
    assert raised.http_status_code == 400
    # The real cause is not swallowed — it appears in the detail.
    assert "connection refused to netbox" in str(raised.detail)
    assert raised.__cause__ is boom
