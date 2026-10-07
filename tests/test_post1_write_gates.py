"""Write-gate tests for HA arm/disarm, custom CPU models, token regenerate and the
encryption key routes."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import (
    ApiKey,
    NetBoxEndpoint,
    ProxmoxEndpoint,
    get_async_session,
    get_session,
)
from proxbox_api.main import app
from proxbox_api.routes.admin import encryption as encryption_module
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
def env(tmp_path: Path) -> Iterator[SimpleNamespace]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False}
    )
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

    state = SimpleNamespace(engine=engine, sessions=[])

    async def _sessions() -> list[FakeSession]:
        return state.sessions

    app.dependency_overrides[get_session] = _sync
    app.dependency_overrides[get_async_session] = _async
    app.dependency_overrides[proxmox_sessions_dep] = _sessions
    with TestClient(app, headers={"X-Proxbox-API-Key": "post1-test-key-0123456789"}) as client:
        state.client = client
        yield state
    app.dependency_overrides.clear()


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
