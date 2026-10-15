"""Write-gate tests for HA arm/disarm, custom CPU models, token regenerate and the
encryption key routes."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api import credentials
from proxbox_api.ceph import v2_routes as ceph_v2_routes
from proxbox_api.credential_writes import (
    commit_credential_write,
    prepare_credential_write,
)
from proxbox_api.database import (
    ApiKey,
    CredentialEncryptionKeyState,
    NetBoxEndpoint,
    ProxmoxEndpoint,
    configure_sqlite_engine,
    get_async_session,
    get_session,
)
from proxbox_api.exception import ProxboxException
from proxbox_api.main import app
from proxbox_api.pbs import admin as pbs_admin
from proxbox_api.pdm import admin as pdm_admin
from proxbox_api.routes import netbox as netbox_routes
from proxbox_api.routes.admin import encryption as encryption_module
from proxbox_api.routes.proxmox import endpoints as proxmox_endpoint_routes
from proxbox_api.session.proxmox_providers import proxmox_sessions_dep

ACTOR = {"X-Proxbox-Actor": "alice"}
SECRET = "s3cr3t-token-value"


class FakeSession:
    """Minimal ProxmoxSession stand-in recording every upstream write."""

    def __init__(
        self,
        name: str,
        endpoint_id: int | None,
        result: object = None,
        source: str | None = "database",
    ) -> None:
        self.name = name
        self.db_endpoint_id = endpoint_id
        self.endpoint_source = source
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self._result = result

    def session(self, path: str) -> SimpleNamespace:
        def make(verb: str) -> AsyncMock:
            async def call(**_kwargs: object) -> object:
                self.calls.append((verb, path))
                if self.error is not None:
                    raise self.error
                return self._result

            return AsyncMock(side_effect=call)

        return SimpleNamespace(post=make("post"), put=make("put"), delete=make("delete"))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY_FILE", str(tmp_path / "encryption.key"))
    credentials.reset_encryption_cache()
    engine = create_engine(
        f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False}
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    async_engine = create_async_engine(
        str(engine.url).replace("sqlite:///", "sqlite+aiosqlite:///"),
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    def _sync() -> Iterator[Session]:
        with Session(engine) as s:
            yield s

    async def _async():  # type: ignore[no-untyped-def]
        async with factory() as s:
            yield s

    with Session(engine) as s:
        ApiKey.store_key(s, "post1-test-key-0123456789", label="post1")

    state = SimpleNamespace(engine=engine, async_engine=async_engine, sessions=[])

    async def _sessions() -> list[FakeSession]:
        return state.sessions

    app.dependency_overrides[get_session] = _sync
    app.dependency_overrides[get_async_session] = _async
    app.dependency_overrides[proxmox_sessions_dep] = _sessions
    with TestClient(app, headers={"X-Proxbox-API-Key": "post1-test-key-0123456789"}) as client:
        state.client = client
        yield state
    app.dependency_overrides.clear()
    credentials.reset_encryption_cache()


def _endpoint(env: SimpleNamespace, name: str, allow_writes: bool) -> int:
    with Session(env.engine) as s:
        ep = ProxmoxEndpoint(
            name=name,
            ip_address="10.0.0.1",
            username="root@pam",
            verify_ssl=False,
            allow_writes=allow_writes,
        )
        s.add(ep)
        s.commit()
        s.refresh(ep)
        assert ep.id is not None
        return ep.id


@pytest.fixture
def proxbox_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    lg = logging.getLogger("proxbox")
    lg.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="proxbox")
    yield caplog
    lg.removeHandler(caplog.handler)


def _app_log(caplog: pytest.LogCaptureFixture) -> str:
    """Application log lines only; the test client's own request log carries the URL."""
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))


_WRITER_ROUTE_CASES = (
    (
        "netbox",
        netbox_routes,
        "/netbox/endpoint",
        {
            "name": "netbox-coordination",
            "ip_address": "1.1.1.1",
            "domain": "",
            "port": 443,
            "token": SECRET,
        },
        200,
    ),
    (
        "proxmox",
        proxmox_endpoint_routes,
        "/proxmox/endpoints",
        {
            "name": "proxmox-coordination",
            "ip_address": "192.0.2.10",
            "port": 8006,
            "username": "root@pam",
            "password": SECRET,
        },
        200,
    ),
    (
        "pbs",
        pbs_admin,
        "/pbs/endpoints",
        {
            "name": "pbs-coordination",
            "host": "pbs.example.test",
            "token_id": "root@pam!sync",
            "token_secret": SECRET,
        },
        200,
    ),
    (
        "pdm",
        pdm_admin,
        "/pdm/endpoints",
        {
            "name": "pdm-coordination",
            "host": "pdm.example.test",
            "token_id": "root@pam!sync",
            "token_secret": SECRET,
        },
        200,
    ),
    (
        "ceph-prometheus",
        ceph_v2_routes,
        "/ceph/v2/metrics/sources",
        {"name": "prom-coordination", "url": "https://prom.example.test", "bearer_token": SECRET},
        201,
    ),
    (
        "ceph-dashboard",
        ceph_v2_routes,
        "/ceph/v2/dashboard/endpoints",
        {
            "name": "dashboard-coordination",
            "base_url": "https://ceph.example.test",
            "username": "admin",
            "password": SECRET,
        },
        201,
    ),
    (
        "ceph-external",
        ceph_v2_routes,
        "/ceph/v2/external/clusters",
        {
            "name": "external-coordination",
            "rgw_admin_url": "https://rgw.example.test",
            "rgw_access_key": SECRET,
            "rgw_secret_key": SECRET,
        },
        201,
    ),
)


@pytest.mark.parametrize(("_name", "module", "path", "payload", "_success"), _WRITER_ROUTE_CASES)
def test_production_credential_writer_rejects_stale_permit(
    env, monkeypatch, _name, module, path, payload, _success
):
    real_prepare = module.prepare_credential_write
    hook_called = False

    async def stale_prepare(session):
        nonlocal hook_called
        hook_called = True
        permit = await real_prepare(session)
        return replace(permit, generation=permit.generation - 1)

    monkeypatch.setattr(module, "prepare_credential_write", stale_prepare)
    response = env.client.post(path, json=payload)

    assert hook_called
    assert response.status_code == 409, response.text
    assert "Retry the request" in response.json()["detail"]
    assert SECRET not in response.text


@pytest.mark.parametrize(("_name", "module", "path", "payload", "_success"), _WRITER_ROUTE_CASES)
def test_credential_writer_preparation_failure_is_secret_free(
    env, monkeypatch, proxbox_log, _name, module, path, payload, _success
):
    hook_called = False

    async def unavailable(_session):
        nonlocal hook_called
        hook_called = True
        raise HTTPException(
            status_code=503,
            detail="Credential encryption coordination is temporarily unavailable.",
        )

    monkeypatch.setattr(module, "prepare_credential_write", unavailable)
    response = env.client.post(path, json=payload)

    assert hook_called
    assert response.status_code == 503, response.text
    assert SECRET not in response.text
    assert SECRET not in _app_log(proxbox_log)


@pytest.mark.parametrize(("_name", "_module", "path", "payload", "success"), _WRITER_ROUTE_CASES)
def test_credential_writer_sql_never_contains_plaintext(
    env, monkeypatch, proxbox_log, _name, _module, path, payload, success
):
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "synthetic-sql-observation-root")
    credentials.reset_encryption_cache()
    monkeypatch.setattr(
        netbox_routes,
        "probe_netbox_endpoint",
        AsyncMock(return_value=SimpleNamespace(reachable=True, status="reachable")),
    )
    statements: list[tuple[str, object]] = []

    def record_sql(_connection, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(env.async_engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        response = env.client.post(path, json=payload)
    finally:
        event.remove(env.async_engine.sync_engine, "before_cursor_execute", record_sql)
        credentials.reset_encryption_cache()

    assert response.status_code == success, response.text
    assert statements
    assert all(SECRET not in repr(item) for item in statements)
    assert SECRET not in response.text
    assert SECRET not in _app_log(proxbox_log)


# ---------------------------------------------------------------------------
# HA arm / disarm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("verb", "path"), [("disarm", "disarm-ha"), ("arm", "arm-ha")])
def test_ha_actor_required(env, verb, path):
    on = FakeSession("on", _endpoint(env, "on", True))
    env.sessions = [on]
    resp = env.client.post(f"/proxmox/cluster/ha/{verb}")
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "actor_required"
    assert on.calls == []


@pytest.mark.parametrize(("verb", "path"), [("disarm", "disarm-ha"), ("arm", "arm-ha")])
def test_ha_per_cluster_gating(env, verb, path, proxbox_log):
    on = FakeSession("on", _endpoint(env, "on", True))
    off = FakeSession("off", _endpoint(env, "off", False))
    orphan = FakeSession("orphan", None)
    env.sessions = [on, off, orphan]
    resp = env.client.post(f"/proxmox/cluster/ha/{verb}", headers=ACTOR)
    assert resp.status_code == 200
    by_name = {r["cluster_name"]: r for r in resp.json()}
    assert by_name["on"]["status"] == "ok"
    for name in ("off", "orphan"):
        assert by_name[name]["status"] == "skipped"
        assert by_name[name]["error"] == "endpoint_writes_disabled"
    assert on.calls == [("post", f"cluster/ha/status/{path}")]
    assert off.calls == [] and orphan.calls == []
    assert "actor=alice" in _app_log(proxbox_log)


@pytest.mark.parametrize(("verb", "path"), [("disarm", "disarm-ha"), ("arm", "arm-ha")])
@pytest.mark.parametrize("source", ["netbox", None])
def test_ha_non_database_session_cannot_borrow_a_local_endpoint_id(env, verb, path, source):
    """A NetBox object id can equal a local ProxmoxEndpoint id; it must never authorise."""
    local_id = _endpoint(env, "local-writable", True)
    other = FakeSession("netbox-cluster", local_id, source=source)
    env.sessions = [other]
    resp = env.client.post(f"/proxmox/cluster/ha/{verb}", headers=ACTOR)
    assert resp.status_code == 200
    (result,) = resp.json()
    assert result["status"] == "skipped" and result["error"] == "endpoint_writes_disabled"
    assert other.calls == []


# ---------------------------------------------------------------------------
# Custom CPU models
# ---------------------------------------------------------------------------

CPU_ROUTES = [
    ("post", "/proxmox/datacenter/cpu-models", {"cputype": "custom-x"}, "post"),
    ("put", "/proxmox/datacenter/cpu-models/custom-x", {"flags": "+aes"}, "put"),
    ("delete", "/proxmox/datacenter/cpu-models/custom-x", None, "delete"),
]


def _send(env, method, url, body, headers=None):
    kwargs = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    return env.client.request(method.upper(), url, **kwargs)


@pytest.mark.parametrize(("method", "url", "body", "verb"), CPU_ROUTES)
def test_cpu_models_denied_when_writes_disabled(env, method, url, body, verb):
    off = FakeSession("off", _endpoint(env, "off", False))
    env.sessions = [off]
    resp = _send(env, method, url, body, ACTOR)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "endpoint_writes_disabled"
    assert off.calls == []


@pytest.mark.parametrize(("method", "url", "body", "verb"), CPU_ROUTES)
def test_cpu_models_actor_required(env, method, url, body, verb):
    on = FakeSession("on", _endpoint(env, "on", True))
    env.sessions = [on]
    resp = _send(env, method, url, body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "actor_required"
    assert on.calls == []


@pytest.mark.parametrize(("method", "url", "body", "verb"), CPU_ROUTES)
def test_cpu_models_success(env, method, url, body, verb):
    on = FakeSession("on", _endpoint(env, "on", True))
    env.sessions = [on]
    resp = _send(env, method, url, body, ACTOR)
    assert resp.status_code == 200, resp.text
    assert [c[0] for c in on.calls] == [verb]


@pytest.mark.parametrize(("method", "url", "body", "verb"), CPU_ROUTES)
def test_cpu_models_upstream_error_not_leaked(env, method, url, body, verb):
    on = FakeSession("on", _endpoint(env, "on", True))
    on.error = RuntimeError(f"boom {SECRET}")
    env.sessions = [on]
    resp = _send(env, method, url, body, ACTOR)
    assert resp.status_code == 502
    assert resp.json()["detail"]["reason"] == "proxmox_upstream_error"
    assert SECRET not in resp.text


# ---------------------------------------------------------------------------
# Token regenerate
# ---------------------------------------------------------------------------

REGEN = "/proxmox/access/tokens/bob@pam/ci/regenerate"


def test_regenerate_denied_when_writes_disabled(env):
    off = FakeSession("off", _endpoint(env, "off", False), {"value": SECRET})
    env.sessions = [off]
    resp = env.client.put(REGEN, headers=ACTOR)
    assert resp.status_code == 403
    assert resp.json()["reason"] == "endpoint_writes_disabled"
    assert off.calls == []


def test_regenerate_actor_required(env):
    on = FakeSession("on", _endpoint(env, "on", True), {"value": SECRET})
    env.sessions = [on]
    resp = env.client.put(REGEN)
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "actor_required"
    assert on.calls == []


def test_regenerate_success_returns_secret_without_logging_it(env, proxbox_log):
    on = FakeSession("on", _endpoint(env, "on", True), {"value": SECRET})
    env.sessions = [on]
    resp = env.client.put(REGEN, headers=ACTOR)
    assert resp.status_code == 200
    assert resp.json()["value"] == SECRET
    assert on.calls == [("put", "access/users/bob@pam/token/ci")]
    assert SECRET not in _app_log(proxbox_log)
    assert "bob@pam" not in _app_log(proxbox_log)
    assert "token/ci" not in _app_log(proxbox_log) and "/ci" not in _app_log(proxbox_log)
    assert "actor=alice" in _app_log(proxbox_log)


def test_regenerate_upstream_error_not_leaked(env, proxbox_log):
    on = FakeSession("on", _endpoint(env, "on", True))
    on.error = RuntimeError(f"boom {SECRET}")
    env.sessions = [on]
    resp = env.client.put(REGEN, headers=ACTOR)
    assert resp.status_code == 502
    assert SECRET not in resp.text and SECRET not in _app_log(proxbox_log)
    assert "bob@pam" not in _app_log(proxbox_log)


# ---------------------------------------------------------------------------
# Encryption key replacement guard
# ---------------------------------------------------------------------------


@pytest.fixture
def key_writes(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(encryption_module, "set_local_encryption_key", mock)
    return mock


def _store_ciphertext(env: SimpleNamespace, where: str) -> None:
    from proxbox_api.database import (
        CephDashboardEndpoint,
        CephExternalCluster,
        PBSEndpoint,
        PDMEndpoint,
        PrometheusSource,
    )

    with Session(env.engine) as s:
        if where == "pbs":
            s.add(PBSEndpoint(name="pbs", host="pbs.example", token_id="t", token_secret="enc:abc"))
        elif where == "pdm":
            s.add(PDMEndpoint(name="pdm", host="pdm.example", token_id="t", token_secret="enc:abc"))
        elif where == "prometheus":
            s.add(PrometheusSource(name="prom", url="https://prom.example", bearer_token="enc:abc"))
        elif where == "ceph-dashboard-password":
            s.add(
                CephDashboardEndpoint(
                    name="cd", base_url="https://ceph.example", password="enc:abc"
                )
            )
        elif where == "ceph-dashboard-token":
            s.add(
                CephDashboardEndpoint(name="cd", base_url="https://ceph.example", token="enc:abc")
            )
        elif where == "ceph-rgw-access":
            s.add(CephExternalCluster(name="cx", rgw_access_key="enc:abc"))
        elif where == "ceph-rgw-secret":
            s.add(CephExternalCluster(name="cx", rgw_secret_key="enc:abc"))
        elif where == "proxmox":
            s.add(
                ProxmoxEndpoint(
                    name="enc",
                    ip_address="10.0.0.9",
                    username="root@pam",
                    password="enc:abc",
                )
            )
        else:
            s.add(NetBoxEndpoint(name="nb", ip_address="10.0.0.8", domain="nb", token="enc:abc"))
        s.commit()


@pytest.mark.parametrize(
    "where",
    [
        "proxmox",
        "netbox",
        "pbs",
        "pdm",
        "prometheus",
        "ceph-dashboard-password",
        "ceph-dashboard-token",
        "ceph-rgw-access",
        "ceph-rgw-secret",
    ],
)
def test_encryption_key_replacement_blocked_by_ciphertext(env, key_writes, where):
    _store_ciphertext(env, where)
    r1 = env.client.post("/admin/encryption/key", json={"key": "a-valid-key-value"})
    r2 = env.client.post("/admin/encryption/generate")
    assert r1.status_code == 409 and r2.status_code == 409
    assert "Encrypted credentials" in r1.json()["detail"]
    key_writes.assert_not_called()


def test_encryption_key_replacement_allowed_without_ciphertext(env, key_writes):
    r1 = env.client.post("/admin/encryption/key", json={"key": "a-valid-key-value"})
    r2 = env.client.post("/admin/encryption/generate")
    assert r1.status_code == 200 and r2.status_code == 200
    assert key_writes.call_count == 2


def test_inflight_credential_commit_rejects_replaced_key(env, monkeypatch, tmp_path):
    key_file = tmp_path / "coordinated-encryption.key"
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY_FILE", str(key_file))
    credentials.reset_encryption_cache()
    credentials.set_local_encryption_key("synthetic-old-key")
    with Session(env.engine) as session:
        session.add(CredentialEncryptionKeyState())
        session.commit()

    async def exercise_race() -> None:
        async_engine = create_async_engine(
            str(env.engine.url).replace("sqlite:///", "sqlite+aiosqlite:///"),
            connect_args={"check_same_thread": False},
        )
        factory = async_sessionmaker(
            async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        try:
            async with factory() as writer:
                generation = await prepare_credential_write(writer)
                endpoint = ProxmoxEndpoint(
                    name="inflight",
                    ip_address="10.0.0.20",
                    username="root@pam",
                )
                endpoint.set_encrypted_password("synthetic-inflight-password")
                writer.add(endpoint)

                with Session(env.engine) as replacer:
                    await encryption_module.set_encryption_key(
                        encryption_module.EncryptionKeyRequest(key="synthetic-new-key"),
                        replacer,
                    )

                with pytest.raises(HTTPException) as raised:
                    await commit_credential_write(writer, generation)
                assert raised.value.status_code == 409
                assert "Retry the request" in raised.value.detail
        finally:
            await async_engine.dispose()

    try:
        asyncio.run(exercise_race())
        with Session(env.engine) as session:
            assert (
                session.exec(
                    select(ProxmoxEndpoint).where(ProxmoxEndpoint.name == "inflight")
                ).first()
                is None
            )
            state = session.get(CredentialEncryptionKeyState, 1)
            assert state is not None and state.generation == 2
    finally:
        credentials.reset_encryption_cache()


def test_failed_key_replacement_leaves_credential_writes_blocked(env, monkeypatch):
    def fail_replacement(_value: str) -> None:
        raise RuntimeError("synthetic key-file replacement failure")

    monkeypatch.setattr(encryption_module, "set_local_encryption_key", fail_replacement)
    with Session(env.engine) as session:
        with pytest.raises(RuntimeError, match="synthetic key-file replacement failure"):
            asyncio.run(
                encryption_module.set_encryption_key(
                    encryption_module.EncryptionKeyRequest(key="synthetic-new-key"),
                    session,
                )
            )

    with Session(env.engine) as session:
        state = session.get(CredentialEncryptionKeyState, 1)
        assert state is not None and state.replacement_pending
        with pytest.raises(HTTPException) as raised:
            asyncio.run(prepare_credential_write(session))
        assert raised.value.status_code == 409
        assert "Retry the request" in raised.value.detail


def test_directory_fsync_failure_keeps_replacement_fence(env, monkeypatch, tmp_path, proxbox_log):
    key_file = tmp_path / "durability-failure.key"
    canary = "synthetic-key-material-canary"
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY_FILE", str(key_file))
    credentials.reset_encryption_cache()

    def fail_directory_fsync(_path: Path) -> None:
        raise OSError("synthetic directory fsync failure")

    monkeypatch.setattr(credentials, "fsync_directory", fail_directory_fsync)
    try:
        response = env.client.post("/admin/encryption/key", json={"key": canary})
        assert response.status_code == 400
        assert canary not in response.text
        assert canary not in _app_log(proxbox_log)
        with Session(env.engine) as session:
            state = session.get(CredentialEncryptionKeyState, 1)
            assert state is not None and state.replacement_pending
    finally:
        credentials.reset_encryption_cache()


def test_key_replacement_lock_contention_is_retryable_and_secret_free():
    session = MagicMock()
    session.exec.side_effect = OperationalError(
        "BEGIN IMMEDIATE", {}, RuntimeError("synthetic lock contention")
    )

    with pytest.raises(HTTPException) as raised:
        encryption_module._begin_serialized_key_write(session)

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}
    assert "synthetic lock contention" not in raised.value.detail
    session.rollback.assert_called_once_with()


def test_retry_lock_timeout_preserves_failed_publication_fence_and_refuses_writer(env, monkeypatch):
    key_file = Path(os.environ["PROXBOX_ENCRYPTION_KEY_FILE"])
    credentials.set_local_encryption_key("synthetic-inherited-old-key")
    real_directory_fsync = credentials.fsync_directory
    with Session(env.engine) as session:
        if session.get(CredentialEncryptionKeyState, 1) is None:
            session.add(CredentialEncryptionKeyState())
            session.commit()

    async def exercise_failure_chain() -> None:
        factory = async_sessionmaker(
            env.async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with factory() as writer:
            permit = await prepare_credential_write(writer)
            endpoint = ProxmoxEndpoint(
                name="delayed-inherited-writer",
                ip_address="192.0.2.31",
                username="root@pam",
            )
            endpoint.set_encrypted_password(
                "synthetic-delayed-old-key-secret", encryptor=permit.encrypt
            )
            writer.add(endpoint)

            def fail_directory_fsync(_path: Path) -> None:
                raise OSError("synthetic publication durability failure")

            monkeypatch.setattr(credentials, "fsync_directory", fail_directory_fsync)
            with Session(env.engine) as replacer:
                with pytest.raises(
                    ProxboxException, match="Could not write local encryption key file"
                ):
                    await encryption_module.set_encryption_key(
                        encryption_module.EncryptionKeyRequest(key="synthetic-inherited-new-key"),
                        replacer,
                    )
            assert key_file.exists()

            monkeypatch.setattr(credentials, "fsync_directory", real_directory_fsync)
            original_begin = encryption_module._begin_serialized_key_write
            begin_calls = 0

            def fail_retry_phase_two(session: Session) -> None:
                nonlocal begin_calls
                begin_calls += 1
                if begin_calls == 2:
                    session.rollback()
                    raise encryption_module._replacement_busy()
                original_begin(session)

            monkeypatch.setattr(
                encryption_module, "_begin_serialized_key_write", fail_retry_phase_two
            )
            with Session(env.engine) as replacer:
                with pytest.raises(HTTPException) as raised:
                    await encryption_module.set_encryption_key(
                        encryption_module.EncryptionKeyRequest(key="synthetic-inherited-retry-key"),
                        replacer,
                    )
            assert raised.value.status_code == 503
            assert raised.value.headers == {"Retry-After": "1"}

            with pytest.raises(HTTPException) as writer_rejected:
                await commit_credential_write(writer, permit)
            assert writer_rejected.value.status_code == 409

    try:
        asyncio.run(exercise_failure_chain())
        with Session(env.engine) as session:
            state = session.get(CredentialEncryptionKeyState, 1)
            assert state is not None
            assert state.generation == 1
            assert state.replacement_pending
            assert (
                session.exec(
                    select(ProxmoxEndpoint).where(
                        ProxmoxEndpoint.name == "delayed-inherited-writer"
                    )
                ).first()
                is None
            )
    finally:
        credentials.reset_encryption_cache()


def test_overlapping_key_replacements_serialize_without_clearing_fence(env, monkeypatch):
    first_replacement_entered = threading.Event()
    release_first_replacement = threading.Event()
    second_begin_entered = threading.Event()
    original_begin = encryption_module._begin_serialized_key_write
    results: list[Exception | None] = []

    def observed_begin(session: Session) -> None:
        if threading.current_thread().name == "second-key-replacement":
            second_begin_entered.set()
        original_begin(session)

    def run_replacement(replacement) -> None:
        try:
            encryption_module._replace_key_serialized(env.engine, replacement)
        except Exception as exc:  # noqa: BLE001 - asserted below
            results.append(exc)
        else:
            results.append(None)

    def blocking_replacement() -> None:
        first_replacement_entered.set()
        assert release_first_replacement.wait(timeout=2)

    monkeypatch.setattr(encryption_module, "_begin_serialized_key_write", observed_begin)
    first = threading.Thread(
        target=run_replacement,
        args=(blocking_replacement,),
        name="first-key-replacement",
    )
    second = threading.Thread(
        target=run_replacement,
        args=(lambda: None,),
        name="second-key-replacement",
    )
    first.start()
    assert first_replacement_entered.wait(timeout=2)
    second.start()
    assert second_begin_entered.wait(timeout=2)
    release_first_replacement.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert results == [None, None]
    with Session(env.engine) as session:
        state = session.get(CredentialEncryptionKeyState, 1)
        assert state is not None
        assert state.generation == 3
        assert not state.replacement_pending


def test_delete_retry_keeps_fence_until_absent_key_directory_is_synced(
    env, monkeypatch, proxbox_log
):
    key_file = Path(os.environ["PROXBOX_ENCRYPTION_KEY_FILE"])
    credentials.set_local_encryption_key("synthetic-delete-durability-key")
    allow_directory_sync = False
    fsync_calls = 0

    def controlled_directory_fsync(_path: Path) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if not allow_directory_sync:
            raise OSError("synthetic delete directory fsync failure")

    monkeypatch.setattr(credentials, "fsync_directory", controlled_directory_fsync)

    first = env.client.delete("/admin/encryption/key")
    assert first.status_code == 400
    assert not key_file.exists()
    with Session(env.engine) as session:
        state = session.get(CredentialEncryptionKeyState, 1)
        assert state is not None and state.replacement_pending

    second = env.client.delete("/admin/encryption/key")
    assert second.status_code == 400
    with Session(env.engine) as session:
        state = session.get(CredentialEncryptionKeyState, 1)
        assert state is not None
        assert state.generation == 1
        assert state.replacement_pending

    allow_directory_sync = True
    third = env.client.delete("/admin/encryption/key")
    assert third.status_code == 200
    with Session(env.engine) as session:
        state = session.get(CredentialEncryptionKeyState, 1)
        assert state is not None
        assert state.generation == 2
        assert not state.replacement_pending
    assert fsync_calls == 3
    assert "synthetic-delete-durability-key" not in _app_log(proxbox_log)


def test_http_key_replacement_waits_off_loop_for_async_writer(env, monkeypatch):
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "synthetic-overlap-original-key")
    credentials.reset_encryption_cache()
    replacement_entered = threading.Event()
    original_replace = encryption_module._replace_key_serialized

    def observed_replace(engine, replacement):
        replacement_entered.set()
        return original_replace(engine, replacement)

    monkeypatch.setattr(encryption_module, "_replace_key_serialized", observed_replace)

    async def exercise_overlap() -> None:
        replacement_app = FastAPI()
        replacement_app.include_router(encryption_module.router, prefix="/admin")

        def replacement_session():
            with Session(env.engine) as session:
                yield session

        replacement_app.dependency_overrides[get_session] = replacement_session
        factory = async_sessionmaker(
            env.async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with factory() as writer:
            permit = await prepare_credential_write(writer)
            endpoint = ProxmoxEndpoint(
                name="overlapping-writer",
                ip_address="192.0.2.30",
                username="root@pam",
            )
            endpoint.set_encrypted_password("synthetic-overlap-secret", encryptor=permit.encrypt)
            writer.add(endpoint)
            fence = text(
                "UPDATE credential_encryption_key_state SET generation = generation "
                "WHERE id = 1 AND generation = :generation AND replacement_pending = 0"
            ).bindparams(generation=permit.generation)
            result = await writer.exec(fence)
            assert result.rowcount == 1

            async with AsyncClient(
                transport=ASGITransport(app=replacement_app),
                base_url="http://testserver",
            ) as client:
                replacement = asyncio.create_task(
                    client.post(
                        "/admin/encryption/key",
                        json={"key": "synthetic-overlap-replacement"},
                    )
                )
                assert await asyncio.to_thread(replacement_entered.wait, 2)
                heartbeat = asyncio.Event()

                async def beat() -> None:
                    await asyncio.sleep(0)
                    heartbeat.set()

                asyncio.create_task(beat())
                await asyncio.wait_for(heartbeat.wait(), timeout=0.5)
                await writer.commit()
                response = await asyncio.wait_for(replacement, timeout=2)

        assert response.status_code == 409
        assert "Encrypted credentials" in response.json()["detail"]

    asyncio.run(exercise_overlap())
    with Session(env.engine) as session:
        endpoint = session.exec(
            select(ProxmoxEndpoint).where(ProxmoxEndpoint.name == "overlapping-writer")
        ).one()
        assert endpoint.password
        state = session.get(CredentialEncryptionKeyState, 1)
        assert state is not None and not state.replacement_pending
    credentials.reset_encryption_cache()
