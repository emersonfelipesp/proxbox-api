"""Router-prefix smoke tests for the proxbox-api FastAPI application.

Drives **every registered router prefix over real HTTP** with FastAPI's
synchronous ``TestClient`` so a mis-wired router, a missing route, or a
mis-placed authentication gate is caught here instead of at runtime.

The app enforces authentication at the **middleware level**
(``APIKeyAuthMiddleware``) before routing, so an unauthenticated ``401`` alone
does not prove a protected route actually exists — a request to a non-existent
path under a protected prefix returns ``401`` too. Coverage therefore combines:

1. **Public routes** (``AUTH_EXEMPT_PATHS``): reachable without an API key.
2. **Protected prefixes**: an unauthenticated request returns ``401`` *and*
   the path template exists in the live OpenAPI schema (so the route is really
   registered, not just shadowed by the auth middleware).
3. **Authenticated dispatch smoke**: a handful of safe read endpoints return
   ``200`` with a valid key, proving end-to-end wiring for prefixes that
   previously had no HTTP-level TestClient coverage (``/auth``, ``/cache``,
   ``/clear-cache``).

Reuses the ``test_client`` (unauthenticated) and ``auth_test_client``
(API-key authenticated) fixtures from ``tests/conftest.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proxbox_api.main import app


def _assert_route_in_openapi(path: str) -> None:
    """Assert a concrete (no path-parameter) route exists in the live OpenAPI.

    Guards against the middleware-401 masking a missing/mis-wired route: the
    auth gate runs before routing, so a 401 by itself does not prove the route
    is registered. All paths asserted here are concrete templates.
    """
    registered = app.openapi()["paths"]
    assert path in registered, (
        f"{path} is not registered in the application OpenAPI schema; "
        f"a protected-route 401 would otherwise hide this wiring gap."
    )


# ---------------------------------------------------------------------------
# Section 1 — Public routes (AUTH_EXEMPT_PATHS): reachable without an API key.
# ---------------------------------------------------------------------------


def test_root_is_public(test_client: TestClient) -> None:
    """The API root is exempt from auth and returns the service banner."""
    resp = test_client.get("/")
    assert resp.status_code == 200


def test_health_is_public(test_client: TestClient) -> None:
    """Health probe is exempt from auth and returns 200."""
    resp = test_client.get("/health")
    assert resp.status_code == 200


def test_openapi_schema_is_public(test_client: TestClient) -> None:
    """The OpenAPI document is served without authentication."""
    resp = test_client.get("/openapi.json")
    assert resp.status_code == 200
    assert "paths" in resp.json()


def test_auth_bootstrap_status_is_public(test_client: TestClient) -> None:
    """Bootstrap-status is exempt so an unconfigured deployment can probe it."""
    resp = test_client.get("/auth/bootstrap-status")
    assert resp.status_code == 200


def test_auth_register_key_is_public_but_requires_body(test_client: TestClient) -> None:
    """register-key is exempt from API-key auth; posting no body is a 422
    (validation), proving the route dispatches rather than being auth-gated."""
    resp = test_client.post("/auth/register-key")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Section 2 — Protected routes: one representative path per registered router
# prefix. Unauthenticated requests must be rejected with 401, and each route
# must exist in the OpenAPI schema (so a 401 cannot mask a missing route).
# ---------------------------------------------------------------------------

PROTECTED_ROUTES: list[tuple[str, str]] = [
    ("GET", "/version"),
    ("GET", "/admin/encryption/status"),
    ("GET", "/auth/keys"),
    ("GET", "/cache"),
    ("GET", "/cache/metrics"),
    ("GET", "/clear-cache"),
    ("GET", "/ceph/status"),
    ("GET", "/dcim/devices"),
    ("GET", "/extras/extras/custom-fields/create"),
    ("POST", "/extras/custom-fields/reconcile"),
    ("GET", "/extras/bootstrap-status"),
    ("POST", "/intent/apply"),
    ("GET", "/netbox/status"),
    ("GET", "/netbox/endpoint"),
    ("POST", "/ssh/sessions"),
    ("GET", "/sync/active"),
    ("GET", "/full-update"),
    ("GET", "/proxmox/"),
    ("GET", "/virtualization/cluster-types/create"),
    ("GET", "/cloud/proxmox-endpoint/by-url"),
    ("GET", "/pbs/endpoints"),
    ("GET", "/pdm/endpoints"),
]


@pytest.mark.parametrize("method,path", PROTECTED_ROUTES)
def test_protected_route_requires_api_key(test_client: TestClient, method: str, path: str) -> None:
    """Every protected prefix rejects an unauthenticated request with 401 and
    is genuinely registered in the OpenAPI schema."""
    resp = test_client.request(method, path)
    assert resp.status_code == 401, (
        f"{method} {path} returned {resp.status_code}; expected 401 "
        f"without an X-Proxbox-API-Key header."
    )
    _assert_route_in_openapi(path)


# ---------------------------------------------------------------------------
# Section 3 — Authenticated dispatch smoke. With a valid API key these safe,
# side-effect-free read endpoints must dispatch to their handler (200), proving
# end-to-end wiring for prefixes that previously lacked HTTP-level coverage.
# ---------------------------------------------------------------------------


def test_version_dispatches_with_api_key(auth_test_client: TestClient) -> None:
    """/version dispatches to its handler and returns a version payload."""
    resp = auth_test_client.get("/version")
    assert resp.status_code == 200
    assert "version" in resp.json()


def test_cache_dispatches_with_api_key(auth_test_client: TestClient) -> None:
    """/cache dispatches end-to-end (previously had no HTTP TestClient cover)."""
    resp = auth_test_client.get("/cache")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_cache_metrics_dispatches_with_api_key(auth_test_client: TestClient) -> None:
    """/cache/metrics dispatches end-to-end and returns a JSON object."""
    resp = auth_test_client.get("/cache/metrics")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_clear_cache_dispatches_with_api_key(auth_test_client: TestClient) -> None:
    """/clear-cache dispatches end-to-end (previously had no HTTP cover)."""
    resp = auth_test_client.get("/clear-cache")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_auth_keys_list_dispatches_with_api_key(auth_test_client: TestClient) -> None:
    """/auth/keys dispatches end-to-end, listing the test key created by the
    fixture (previously the /auth/* HTTP surface had no TestClient cover)."""
    resp = auth_test_client.get("/auth/keys")
    assert resp.status_code == 200
