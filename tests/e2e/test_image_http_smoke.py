"""HTTP/HTTPS smoke tests against a running proxbox-api Docker image (nginx → uvicorn).

Set ``PROXBOX_IMAGE_E2E_BASE_URL`` (e.g. ``http://127.0.0.1:18080`` or ``https://127.0.0.1:18443``).
For the mkcert image use HTTPS and set ``PROXBOX_IMAGE_E2E_TLS_INSECURE=1`` (runner does not trust mkcert CA).

These tests are excluded from the NetBox demo E2E phase (``-m "not image_http"``) so the
demo instance is not hammered by redundant API traffic.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.image_http


@pytest.fixture
def image_base_url() -> str:
    base = os.environ.get("PROXBOX_IMAGE_E2E_BASE_URL", "").strip().rstrip("/")
    if not base:
        pytest.fail("PROXBOX_IMAGE_E2E_BASE_URL must be set for image_http tests")
    return base


@pytest.fixture
def image_http_client(image_base_url: str) -> httpx.Client:
    insecure = os.environ.get("PROXBOX_IMAGE_E2E_TLS_INSECURE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    with httpx.Client(
        base_url=image_base_url,
        verify=not insecure,
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        yield client


def test_image_root_returns_json(image_http_client: httpx.Client) -> None:
    response = image_http_client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert "message" in body


def test_image_docs_available(image_http_client: httpx.Client) -> None:
    response = image_http_client.get("/docs")
    assert response.status_code == 200
    assert "swagger" in response.text.lower() or "openapi" in response.text.lower()


def test_image_openapi_json(image_http_client: httpx.Client) -> None:
    response = image_http_client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec.get("openapi")
    assert "paths" in spec
