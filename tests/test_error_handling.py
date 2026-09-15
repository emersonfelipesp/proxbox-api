"""Tests for global error handling and validation edge cases."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from proxmox_sdk.sdk.exceptions import ResourceException
from sqlmodel import Session

from proxbox_api.app.exceptions import register_exception_handlers
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.schemas.proxmox import ProxmoxSessionSchema
from proxbox_api.session.proxmox import ProxmoxSession, proxmox_sessions
from proxbox_api.session.proxmox_providers import (
    _close_sessions_after_failed_acquisition,
    _create_all_sessions,
    _session_acquisition_error,
)


def test_unhandled_exception_hides_internal_detail_by_default(monkeypatch):
    monkeypatch.delenv("PROXBOX_EXPOSE_INTERNAL_ERRORS", raising=False)
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret-internal-token")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "An unexpected error occurred."
    assert body["python_exception"] is None
    assert "secret-internal-token" not in response.text


def test_unhandled_exception_exposes_detail_when_flag_set(monkeypatch):
    monkeypatch.setenv("PROXBOX_EXPOSE_INTERNAL_ERRORS", "1")
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def boom():
        raise RuntimeError("visible-error")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert "visible-error" in body["detail"]


def test_proxbox_exception_exposes_only_explicit_safe_exception_type(monkeypatch):
    monkeypatch.delenv("PROXBOX_EXPOSE_INTERNAL_ERRORS", raising=False)
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/safe-error")
    def safe_error():
        raise ProxboxException(
            message="Session acquisition failed",
            python_exception="secret-internal-token",
            public_python_exception="RuntimeError",
            http_status_code=502,
        )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/safe-error")

    assert response.status_code == 502
    assert response.json()["python_exception"] == "RuntimeError"
    assert "secret-internal-token" not in response.text


def test_session_acquisition_error_forwards_only_sdk_http_status() -> None:
    upstream = ResourceException(503, "Service Unavailable", "private upstream body")
    translated = _session_acquisition_error(upstream)

    assert translated.http_status_code == 503
    assert translated.detail == {
        "reason": "proxmox_session_acquisition_failed",
        "error_type": "ResourceException",
        "upstream_status": 503,
    }
    assert translated.public_python_exception == "ResourceException"

    class MisleadingFailure(RuntimeError):
        status_code = 418

    generic = _session_acquisition_error(MisleadingFailure("private internal body"))
    assert generic.http_status_code == 502
    assert generic.detail == {
        "reason": "proxmox_session_acquisition_failed",
        "error_type": "MisleadingFailure",
    }


def test_proxmox_sessions_rejects_invalid_endpoint_ids(db_engine):
    with Session(db_engine) as session:
        session.add(
            ProxmoxEndpoint(
                name="pve01",
                ip_address="10.0.0.10",
                domain="pve.local",
                port=8006,
                username="root@pam",
                password="password",
                verify_ssl=False,
            )
        )
        session.commit()

        with pytest.raises(ProxboxException, match="Invalid Proxmox endpoint_ids"):
            asyncio.run(proxmox_sessions(session, endpoint_ids="1,not-an-int"))


def test_proxmox_sessions_translate_constructor_failure(monkeypatch, db_engine):
    async def fail_create(*_args, **_kwargs):
        raise RuntimeError("secret-internal-token")

    monkeypatch.setattr(ProxmoxSession, "create", fail_create)
    with Session(db_engine) as session:
        session.add(
            ProxmoxEndpoint(
                name="pve01",
                ip_address="10.0.0.10",
                domain="pve.local",
                port=8006,
                username="root@pam",
                password="password",
                verify_ssl=False,
            )
        )
        session.commit()

        with pytest.raises(ProxboxException) as exc_info:
            asyncio.run(proxmox_sessions(session))

    error = exc_info.value
    assert error.http_status_code == 502
    assert error.detail == {
        "reason": "proxmox_session_acquisition_failed",
        "error_type": "RuntimeError",
    }
    assert error.public_python_exception == "RuntimeError"
    assert error.python_exception == "secret-internal-token"


@pytest.mark.asyncio
async def test_multi_endpoint_failure_closes_successful_and_failed_sessions(monkeypatch):
    closed: list[str] = []

    class FakeSDK:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            closed.append(self.name)

    async def initialize(self, config, *, initialize_metadata=True):
        del initialize_metadata
        self.session = FakeSDK(config.name)
        self.proxmox = self.session
        if config.name == "broken":
            raise RuntimeError("session initialization failed")

    monkeypatch.setattr(ProxmoxSession, "_initialize", initialize)
    schemas = [
        ProxmoxSessionSchema(name="healthy"),
        ProxmoxSessionSchema(name="broken"),
    ]

    with pytest.raises(ProxboxException) as exc_info:
        await _create_all_sessions(schemas)

    assert exc_info.value.http_status_code == 502
    assert sorted(closed) == ["broken", "healthy"]


@pytest.mark.asyncio
async def test_multi_endpoint_failure_closes_every_session_once_despite_base_exception(
    monkeypatch,
):
    close_attempts: list[str] = []
    failures = {
        "first-broken": RuntimeError("first failure"),
        "second-broken": ValueError("second failure"),
    }

    class FakeSession:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            close_attempts.append(self.name)
            if self.name == "cancel-on-close":
                raise asyncio.CancelledError

    async def create(schema):
        if schema.name in failures:
            raise failures[schema.name]
        return FakeSession(schema.name)

    monkeypatch.setattr(ProxmoxSession, "create", create)
    schemas = [
        ProxmoxSessionSchema(name="first-broken"),
        ProxmoxSessionSchema(name="cancel-on-close"),
        ProxmoxSessionSchema(name="healthy"),
        ProxmoxSessionSchema(name="second-broken"),
    ]

    with pytest.raises(ProxboxException) as exc_info:
        await _create_all_sessions(schemas)

    assert exc_info.value.python_exception == "first failure"
    assert sorted(close_attempts) == ["cancel-on-close", "healthy"]


@pytest.mark.asyncio
async def test_failed_acquisition_cleanup_finishes_before_redelivering_cancellation():
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    closed: list[str] = []

    class SlowSession:
        async def aclose(self) -> None:
            cleanup_started.set()
            await allow_cleanup.wait()
            closed.append("slow")

    cleanup_task = asyncio.create_task(_close_sessions_after_failed_acquisition([SlowSession()]))
    await cleanup_started.wait()
    cleanup_task.cancel()
    await asyncio.sleep(0)
    assert not cleanup_task.done()

    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup_task

    assert closed == ["slow"]
