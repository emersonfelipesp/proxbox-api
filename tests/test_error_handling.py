"""Tests for global error handling and validation edge cases."""

# ruff: noqa: ANN201, D103

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from proxbox_api.app.exceptions import register_exception_handlers
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.session.proxmox import proxmox_sessions


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
