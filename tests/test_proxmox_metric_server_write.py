"""Tests for the write-gated Proxmox InfluxDB metric-server route."""

from __future__ import annotations

import json
import logging
import re

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.routes.proxmox import metrics as metrics_route
from proxbox_api.schemas.proxmox_metric_servers import (
    CONFIG_ID_PATTERN,
    InfluxMetricServerUpdate,
)

SECRET = "super-secret-influx-token"


@pytest.fixture
def proxbox_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture the non-propagating ``proxbox`` logger so log assertions are not vacuous."""
    logger = logging.getLogger("proxbox")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="proxbox")
    yield caplog
    logger.removeHandler(caplog.handler)


class _Session:
    def __init__(self, endpoint: ProxmoxEndpoint | None) -> None:
        self.endpoint = endpoint

    async def get(self, model: object, object_id: int) -> ProxmoxEndpoint | None:
        if model is ProxmoxEndpoint and self.endpoint and object_id == self.endpoint.id:
            return self.endpoint
        return None


class _Resource:
    def __init__(self, owner: _FakeProxmox, path: str) -> None:
        self.owner = owner
        self.path = path

    async def put(self, **body: object) -> None:
        self.owner.calls.append((self.path, body))
        if self.owner.fail:
            raise RuntimeError(f"upstream rejected token {SECRET}")


class _FakeProxmox:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail = fail
        self.closed = False

    def session(self, path: str) -> _Resource:
        return _Resource(self, path)

    async def aclose(self) -> None:
        self.closed = True


def _endpoint(*, allow_writes: bool) -> ProxmoxEndpoint:
    return ProxmoxEndpoint(
        id=7,
        name="pve-lab",
        ip_address="10.0.0.10",
        port=8006,
        username="root@pam",
        verify_ssl=False,
        allow_writes=allow_writes,
    )


_VALID = {"server": "influx.example.test", "port": 8086, "token": SECRET}


def _body(**overrides: object) -> InfluxMetricServerUpdate:
    values: dict[str, object] = {"server": "influx.example.test", "port": 8086, "token": SECRET}
    values.update(overrides)
    return InfluxMetricServerUpdate.model_validate(values)


def _patch_open(monkeypatch: pytest.MonkeyPatch, proxmox: _FakeProxmox) -> list[object]:
    opened: list[object] = []

    async def _open(endpoint: object) -> _FakeProxmox:
        opened.append(endpoint)
        return proxmox

    monkeypatch.setattr(metrics_route, "_open_proxmox_session", _open)
    return opened


async def _call(session: _Session, **overrides: object) -> object:
    kwargs: dict[str, object] = {
        "config_id": "influx-1",
        "payload": _body(),
        "database_session": session,
        "endpoint_id": 7,
        "actor": "operator",
    }
    kwargs.update(overrides)
    return await metrics_route.update_influx_metric_server(**kwargs)  # type: ignore[arg-type]


async def test_denied_when_endpoint_writes_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    proxmox = _FakeProxmox()
    opened = _patch_open(monkeypatch, proxmox)
    response = await _call(_Session(_endpoint(allow_writes=False)))
    assert isinstance(response, JSONResponse) and response.status_code == 403
    assert json.loads(response.body)["reason"] == "endpoint_writes_disabled"
    assert not opened and not proxmox.calls


async def test_denied_without_endpoint_id(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _patch_open(monkeypatch, _FakeProxmox())
    response = await _call(_Session(_endpoint(allow_writes=True)), endpoint_id=None)
    assert isinstance(response, JSONResponse) and response.status_code == 403
    assert json.loads(response.body)["reason"] == "endpoint_id_required"
    assert not opened


async def test_unknown_endpoint_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _patch_open(monkeypatch, _FakeProxmox())
    response = await _call(_Session(None))
    assert isinstance(response, JSONResponse)
    assert json.loads(response.body)["reason"] == "endpoint_not_found"
    assert not opened


@pytest.mark.parametrize("actor", [None, "", "   "])
async def test_actor_header_is_required(monkeypatch: pytest.MonkeyPatch, actor: str | None) -> None:
    opened = _patch_open(monkeypatch, _FakeProxmox())
    with pytest.raises(HTTPException) as caught:
        await _call(_Session(_endpoint(allow_writes=True)), actor=actor)
    assert caught.value.status_code == 422
    assert caught.value.detail["reason"] == "actor_required"
    assert not opened


async def test_success_writes_allow_listed_body_and_never_echoes_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxmox = _FakeProxmox()
    _patch_open(monkeypatch, proxmox)
    response = await _call(
        _Session(_endpoint(allow_writes=True)),
        payload=_body(influxdbproto="https", verify_certificate=True),
    )
    assert proxmox.calls == [
        (
            "cluster/metrics/server/influx-1",
            {
                "server": "influx.example.test",
                "port": 8086,
                "influxdbproto": "https",
                "token": SECRET,
                "verify-certificate": 1,
            },
        )
    ]
    assert proxmox.closed
    dumped = response.model_dump_json()
    assert SECRET not in dumped
    assert response.fields == sorted(
        ["server", "port", "influxdbproto", "token", "verify-certificate"]
    )
    assert response.actor == "operator" and response.cluster_name == "pve-lab"


async def test_upstream_failure_is_secret_safe(
    monkeypatch: pytest.MonkeyPatch, proxbox_logs: pytest.LogCaptureFixture
) -> None:
    proxmox = _FakeProxmox(fail=True)
    _patch_open(monkeypatch, proxmox)
    with pytest.raises(HTTPException) as caught:
        await _call(_Session(_endpoint(allow_writes=True)))
    assert caught.value.status_code == 502
    assert SECRET not in json.dumps(caught.value.detail)
    assert "write failed" in proxbox_logs.text
    assert SECRET not in proxbox_logs.text
    assert "influx.example.test" not in proxbox_logs.text
    assert proxmox.closed


async def test_session_open_failure_is_secret_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_endpoint: object) -> None:
        raise RuntimeError(f"cannot reach host with {SECRET}")

    monkeypatch.setattr(metrics_route, "_open_proxmox_session", _boom)
    with pytest.raises(HTTPException) as caught:
        await _call(_Session(_endpoint(allow_writes=True)))
    assert caught.value.status_code == 502
    assert SECRET not in json.dumps(caught.value.detail)


@pytest.mark.parametrize("config_id", ["influx-1", "a", "Metrics_01"])
def test_config_id_accepts_plain_identifiers(config_id: str) -> None:
    assert re.fullmatch(CONFIG_ID_PATTERN, config_id)


@pytest.mark.parametrize(
    "config_id", ["", "../cluster", "a/b", "a b", "-lead", "a?x=1", "a" * 65, "%2e%2e"]
)
def test_config_id_rejects_path_manipulation(config_id: str) -> None:
    assert not re.fullmatch(CONFIG_ID_PATTERN, config_id)


def test_body_rejects_unknown_and_invalid_fields() -> None:
    InfluxMetricServerUpdate.model_validate(_VALID)
    for bad in (
        {"delete": "token"},
        {"port": 70000},
        {"server": "bad host/../x"},
        {"influxdbproto": "ftp"},
    ):
        with pytest.raises(ValidationError):
            InfluxMetricServerUpdate.model_validate({**_VALID, **bad})


def test_token_is_hidden_from_repr_and_dump() -> None:
    body = _body()
    assert SECRET not in repr(body)
    assert SECRET not in body.model_dump_json()
    assert body.pve_payload()["token"] == SECRET


def _client(session: _Session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from proxbox_api.database import get_async_session

    app = FastAPI()
    app.include_router(metrics_route.router, prefix="/proxmox/metrics")

    async def _override() -> object:
        yield session

    app.dependency_overrides[get_async_session] = _override
    return TestClient(app)


_URL = "/proxmox/metrics/influx/servers/{}?endpoint_id=7"
_HEADERS = {"X-Proxbox-Actor": "operator"}


@pytest.mark.parametrize("config_id", ["a%20b", "-lead", "%2e%2e", "a" * 65])
def test_http_rejects_bad_config_id_before_any_write(
    monkeypatch: pytest.MonkeyPatch, config_id: str
) -> None:
    proxmox = _FakeProxmox()
    opened = _patch_open(monkeypatch, proxmox)
    response = _client(_Session(_endpoint(allow_writes=True))).put(
        _URL.format(config_id), json={**_VALID}, headers=_HEADERS
    )
    assert response.status_code in {404, 422}
    assert not opened and not proxmox.calls


def test_http_rejects_unknown_field_before_any_write(monkeypatch: pytest.MonkeyPatch) -> None:
    proxmox = _FakeProxmox()
    opened = _patch_open(monkeypatch, proxmox)
    response = _client(_Session(_endpoint(allow_writes=True))).put(
        _URL.format("influx-1"),
        json={**_VALID, "delete": "token"},
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert not opened and not proxmox.calls


def test_http_gate_and_success_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    proxmox = _FakeProxmox()
    _patch_open(monkeypatch, proxmox)
    denied = _client(_Session(_endpoint(allow_writes=False))).put(
        _URL.format("influx-1"), json={**_VALID}, headers=_HEADERS
    )
    assert denied.status_code == 403 and not proxmox.calls
    ok = _client(_Session(_endpoint(allow_writes=True))).put(
        _URL.format("influx-1"), json={**_VALID}, headers=_HEADERS
    )
    assert ok.status_code == 200 and SECRET not in ok.text
    assert proxmox.calls[0][0] == "cluster/metrics/server/influx-1"


@pytest.mark.parametrize("missing", ["server", "port", "token"])
def test_server_port_and_token_are_required(missing: str) -> None:
    body = {key: value for key, value in _VALID.items() if key != missing}
    with pytest.raises(ValidationError, match=missing):
        InfluxMetricServerUpdate.model_validate(body)


@pytest.mark.parametrize(
    "extra",
    [
        {"organization": "other-tenant"},
        {"bucket": "other-bucket"},
        {"verify-certificate": False},
        {"influxdbproto": "https"},
        {"api-path-prefix": "/x"},
        {"disable": True, "timeout": 5},
    ],
)
def test_optional_settings_are_sent_with_the_required_trio(extra: dict[str, object]) -> None:
    payload = InfluxMetricServerUpdate.model_validate({**_VALID, **extra}).pve_payload()
    assert {"server", "port", "token"} <= set(payload)


async def test_write_is_audited_without_values(
    monkeypatch: pytest.MonkeyPatch, proxbox_logs: pytest.LogCaptureFixture
) -> None:
    _patch_open(monkeypatch, _FakeProxmox())
    await _call(_Session(_endpoint(allow_writes=True)))
    assert "actor=operator endpoint=7 config=influx-1" in proxbox_logs.text
    assert SECRET not in proxbox_logs.text and "influx.example.test" not in proxbox_logs.text


@pytest.mark.parametrize(
    "body",
    [
        {"verify-certificate": False},
        {"bucket": "other-bucket"},
        {"server": "h", "port": 8086},
        {"server": "h", "token": SECRET},
        {"port": 8086, "token": SECRET},
    ],
)
def test_http_partial_or_tokenless_update_is_rejected_before_any_session(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    proxmox = _FakeProxmox()
    opened = _patch_open(monkeypatch, proxmox)
    response = _client(_Session(_endpoint(allow_writes=True))).put(
        _URL.format("influx-1"), json=body, headers=_HEADERS
    )
    assert response.status_code == 422
    assert not opened and not proxmox.calls


@pytest.mark.parametrize(
    "field",
    [
        {"organization": "proxmox&bucket=other"},
        {"organization": "a\nb"},
        {"bucket": "x/../y"},
        {"api-path-prefix": "/v2?org=evil"},
        {"api-path-prefix": "//evil"},
    ],
)
def test_query_and_path_delimiters_are_rejected(field: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InfluxMetricServerUpdate.model_validate({**_VALID, **field})


def test_plain_names_and_prefix_are_accepted() -> None:
    body = InfluxMetricServerUpdate.model_validate(
        {
            **_VALID,
            "organization": "ops team",
            "bucket": "proxmox.metrics-1",
            "api-path-prefix": "/influx/v2",
        }
    )
    assert body.pve_payload()["bucket"] == "proxmox.metrics-1"


async def test_failed_write_is_audited_as_possibly_partial(
    monkeypatch: pytest.MonkeyPatch, proxbox_logs: pytest.LogCaptureFixture
) -> None:
    _patch_open(monkeypatch, _FakeProxmox(fail=True))
    with pytest.raises(HTTPException) as caught:
        await _call(_Session(_endpoint(allow_writes=True)))
    text = proxbox_logs.text
    assert "write attempt: actor=operator endpoint=7 config=influx-1" in text
    assert "possibly partially applied: actor=operator endpoint=7 config=influx-1" in text
    assert "applied: actor" not in text.replace("possibly partially applied: actor", "")
    assert "partially applied" in str(caught.value.detail)
    assert SECRET not in text and "influx.example.test" not in text


async def test_session_failure_is_audited_as_not_applied(
    monkeypatch: pytest.MonkeyPatch, proxbox_logs: pytest.LogCaptureFixture
) -> None:
    async def _boom(_endpoint: object) -> None:
        raise RuntimeError("down")

    monkeypatch.setattr(metrics_route, "_open_proxmox_session", _boom)
    with pytest.raises(HTTPException):
        await _call(_Session(_endpoint(allow_writes=True)))
    assert "write attempt: actor=operator" in proxbox_logs.text
    assert "not applied (no session): actor=operator" in proxbox_logs.text


async def test_cancellation_is_audited_and_propagates(
    monkeypatch: pytest.MonkeyPatch, proxbox_logs: pytest.LogCaptureFixture
) -> None:
    import asyncio

    proxmox = _FakeProxmox()

    class _Cancelled(_Resource):
        async def put(self, **body: object) -> None:
            raise asyncio.CancelledError

    monkeypatch.setattr(proxmox, "session", lambda path: _Cancelled(proxmox, path))
    _patch_open(monkeypatch, proxmox)
    with pytest.raises(asyncio.CancelledError):
        await _call(_Session(_endpoint(allow_writes=True)))
    assert "possibly partially applied" in proxbox_logs.text
    assert proxmox.closed
