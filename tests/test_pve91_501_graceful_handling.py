"""Tests for graceful HTTP 501 handling on PVE < 9.2 endpoints.

On Proxmox VE 9.1.x the following endpoints are not implemented:
  - GET /cluster/sdn/fabrics
  - GET /cluster/sdn/fabrics/all
  - GET /cluster/sdn/route-maps
  - GET /cluster/sdn/prefix-lists
  - GET /cluster/qemu/custom-cpu-models

proxmox-sdk raises ResourceException(status_code=501) for each.
The routes must log at WARNING (not ERROR with traceback) and return
an empty list for that cluster rather than an error sentinel entry.

See: https://github.com/emersonfelipesp/proxbox-api/issues/158
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from proxmox_sdk.sdk.exceptions import ResourceException

from proxbox_api.routes.proxmox.datacenter import list_custom_cpu_models
from proxbox_api.routes.proxmox.sdn import (
    sdn_fabrics,
    sdn_fabrics_all,
    sdn_prefix_lists,
    sdn_route_maps,
)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _make_501() -> ResourceException:
    return ResourceException(
        status_code=501,
        status_message="Not Implemented",
        content="Method not implemented",
    )


class _FakePx:
    """Minimal Proxmox session fake that exercises the route handler.

    The route handlers call ``px.session(path).get()`` which returns a
    coroutine. ``resolve_async`` then awaits it. Set ``exc`` to have the
    coroutine raise, or ``rows`` to have it return a list.
    """

    def __init__(self, name: str, *, exc: Exception | None = None, rows: list | None = None):
        self.name = name
        self._exc = exc
        self._rows = rows or []

    def session(self, path: str):
        exc = self._exc
        rows = list(self._rows)

        class _Resource:
            async def get(self) -> list:  # type: ignore[override]
                if exc is not None:
                    raise exc
                return rows

        return _Resource()


@pytest.fixture
def proxbox_caplog(caplog):
    """Attach caplog to the non-propagating ``proxbox`` logger."""
    proxbox_logger = logging.getLogger("proxbox")
    proxbox_logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        proxbox_logger.removeHandler(caplog.handler)


# ---------------------------------------------------------------------------
# sdn.py — 501 on PVE < 9.2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler,endpoint_fragment",
    [
        (sdn_fabrics, "/cluster/sdn/fabrics"),
        (sdn_fabrics_all, "/cluster/sdn/fabrics/all"),
        (sdn_route_maps, "/cluster/sdn/route-maps"),
        (sdn_prefix_lists, "/cluster/sdn/prefix-lists"),
    ],
)
def test_sdn_501_returns_empty_list(handler, endpoint_fragment):
    """HTTP 501 from PVE yields an empty result list (no error sentinel)."""
    pxs = [_FakePx("pve91", exc=_make_501())]
    result = _run(handler(pxs))
    assert result == [], f"Expected empty list for {endpoint_fragment}, got {result}"


@pytest.mark.parametrize(
    "handler",
    [sdn_fabrics, sdn_fabrics_all, sdn_route_maps, sdn_prefix_lists],
)
def test_sdn_501_logs_warning_not_error(handler, proxbox_caplog):
    """HTTP 501 must produce a WARNING, not an ERROR."""
    pxs = [_FakePx("pve91", exc=_make_501())]
    proxbox_caplog.set_level(logging.DEBUG)

    _run(handler(pxs))

    error_records = [r for r in proxbox_caplog.records if r.levelno >= logging.ERROR]
    assert not error_records, f"Unexpected ERROR records: {error_records}"

    warning_records = [r for r in proxbox_caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected at least one WARNING record for 501"
    assert any("PVE < 9.2" in r.message for r in warning_records)


@pytest.mark.parametrize(
    "handler",
    [sdn_fabrics, sdn_prefix_lists],
)
def test_sdn_non_501_still_logs_error(handler, proxbox_caplog):
    """Non-501 exceptions keep ERROR-level logging."""
    pxs = [_FakePx("pve91", exc=RuntimeError("connection refused"))]
    proxbox_caplog.set_level(logging.DEBUG)

    _run(handler(pxs))

    error_records = [r for r in proxbox_caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "Expected ERROR records for non-501 exception"


@pytest.mark.parametrize(
    "handler",
    [sdn_fabrics, sdn_prefix_lists],
)
def test_sdn_non_501_still_returns_error_entry(handler):
    """Non-501 exceptions produce an error sentinel entry in the response."""
    pxs = [_FakePx("mycluster", exc=RuntimeError("connection refused"))]

    result = _run(handler(pxs))
    assert len(result) == 1
    assert result[0].status == "error"
    assert result[0].cluster_name == "mycluster"


def test_sdn_501_multi_cluster_only_skips_affected():
    """501 from one cluster does not suppress results from healthy clusters."""
    pxs = [
        _FakePx("good", rows=[{"fabric": "wg0", "type": "vxlan"}]),
        _FakePx("bad", exc=_make_501()),
    ]

    result = _run(sdn_fabrics(pxs))
    assert len(result) == 1
    assert result[0].cluster_name == "good"
    assert result[0].fabric == "wg0"


def test_sdn_prefix_list_501_does_not_pollute_other_results():
    """501 cluster does not appear in prefix-list results at all."""
    pxs = [
        _FakePx("pve92", rows=[{"name": "allow-rfc1918", "cidr": "10.0.0.0/8", "action": "permit"}]),
        _FakePx("pve91", exc=_make_501()),
    ]

    result = _run(sdn_prefix_lists(pxs))
    assert len(result) == 1
    assert result[0].name == "allow-rfc1918"
    assert result[0].cluster_name == "pve92"


# ---------------------------------------------------------------------------
# datacenter.py — 501 on PVE < 9.2
# ---------------------------------------------------------------------------


def test_cpu_models_501_returns_empty_list():
    """HTTP 501 from PVE yields an empty result list for cpu-models."""
    pxs = [_FakePx("pve91", exc=_make_501())]
    result = _run(list_custom_cpu_models(pxs))
    assert result == []


def test_cpu_models_501_logs_warning_not_error(proxbox_caplog):
    """HTTP 501 for cpu-models must produce a WARNING, not ERROR."""
    pxs = [_FakePx("pve91", exc=_make_501())]
    proxbox_caplog.set_level(logging.DEBUG)

    _run(list_custom_cpu_models(pxs))

    error_records = [r for r in proxbox_caplog.records if r.levelno >= logging.ERROR]
    assert not error_records

    warning_records = [r for r in proxbox_caplog.records if r.levelno == logging.WARNING]
    assert warning_records
    assert any("PVE < 9.2" in r.message for r in warning_records)


def test_cpu_models_non_501_still_logs_error(proxbox_caplog):
    """Non-501 exceptions for cpu-models keep ERROR-level logging."""
    pxs = [_FakePx("pve91", exc=RuntimeError("timeout"))]
    proxbox_caplog.set_level(logging.DEBUG)

    _run(list_custom_cpu_models(pxs))

    error_records = [r for r in proxbox_caplog.records if r.levelno >= logging.ERROR]
    assert error_records


def test_cpu_models_non_501_returns_error_entry():
    """Non-501 exceptions for cpu-models produce an error sentinel entry."""
    pxs = [_FakePx("mycluster", exc=RuntimeError("timeout"))]

    result = _run(list_custom_cpu_models(pxs))
    assert len(result) == 1
    assert result[0].status == "error"
    assert result[0].cluster_name == "mycluster"


def test_cpu_models_501_multi_cluster():
    """501 only skips the failing cluster; healthy cluster data is preserved."""
    pxs = [
        _FakePx(
            "pve92",
            rows=[{"cputype": "custom-icelake", "base-cputype": "Icelake-Server"}],
        ),
        _FakePx("pve91", exc=_make_501()),
    ]

    result = _run(list_custom_cpu_models(pxs))
    assert len(result) == 1
    assert result[0].cputype == "custom-icelake"
    assert result[0].cluster_name == "pve92"
