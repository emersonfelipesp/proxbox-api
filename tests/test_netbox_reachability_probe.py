"""NetBox reachability probe response, cache, and fail-fast contracts."""

from __future__ import annotations

import asyncio
import math
import multiprocessing
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from netbox_sdk.config import Config

from proxbox_api import netbox_probe
from proxbox_api.database import NetBoxEndpoint
from proxbox_api.dependencies import ensure_netbox_sync_dependencies, proxbox_tag
from proxbox_api.exception import ProxboxException


class _FakeClient:
    payload: object = {"netbox-version": "4.6.1"}
    error: BaseException | None = None
    created: list[_FakeClient] = []

    def __init__(self, config: Config) -> None:
        self.config = config
        self.closed = 0
        self.created.append(self)

    async def status(self) -> object:
        if self.error is not None:
            raise self.error
        return self.payload

    async def close(self) -> None:
        self.closed += 1


class _FakeApi:
    def __init__(self, client: _FakeClient, schema: object = None) -> None:
        self.client = client
        self.schema = schema

    async def status(self) -> object:
        return await self.client.status()


def _read_probe_in_child(
    queue: multiprocessing.Queue, ready: Any, start: Any, cache_path: str
) -> None:
    netbox_probe._cache_path = lambda: Path(cache_path)
    config = Config(base_url="https://netbox.example.test", token_secret="shared")
    ready.set()
    start.wait()
    result = netbox_probe.recent_probe(config)
    queue.put(result.model_dump() if result is not None else None)


def _endpoint() -> NetBoxEndpoint:
    return NetBoxEndpoint(
        id=41,
        name="netbox",
        ip_address="2001:db8::41",
        domain="netbox.example.test",
        port=443,
        token_version="v1",
        token="probe-secret",
        verify_ssl=True,
    )


@pytest.fixture(autouse=True)
def _isolated_probe(monkeypatch: pytest.MonkeyPatch, tmp_path):
    cache_path = tmp_path / "netbox-probe-cache.json"
    monkeypatch.setattr(netbox_probe, "_cache_path", lambda: cache_path)
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / "proxbox.db"))
    monkeypatch.setenv("PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY", "1")
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    netbox_probe.clear_probe_cache()
    _FakeClient.created = []
    _FakeClient.payload = {"netbox-version": "4.6.1"}
    _FakeClient.error = None
    monkeypatch.setattr(netbox_probe, "NetBoxApiClient", _FakeClient)
    monkeypatch.setattr(netbox_probe, "Api", _FakeApi)
    monkeypatch.setattr(netbox_probe, "build_schema_index", lambda **_kwargs: None)
    yield
    netbox_probe.clear_probe_cache()


@pytest.mark.asyncio
async def test_probe_reports_version_uses_short_timeout_and_closes() -> None:
    result = await netbox_probe.probe_netbox_endpoint(_endpoint())

    assert result.model_dump() == {
        "reachable": True,
        "status": "reachable",
        "api_version": "4.6.1",
        "error_type": None,
        "error": None,
        "timeout_seconds": 10.0,
    }
    assert _FakeClient.created[0].config.timeout == 10.0
    assert _FakeClient.created[0].closed == 1


@pytest.mark.asyncio
async def test_probe_result_survives_shared_cache_write_failure(monkeypatch) -> None:
    monkeypatch.setattr(netbox_probe, "_store", lambda *_args: (_ for _ in ()).throw(OSError()))
    result = await netbox_probe.probe_netbox_endpoint(_endpoint())
    assert result.reachable is True


@pytest.mark.asyncio
async def test_probe_caches_credential_safe_timeout_for_fast_failure() -> None:
    _FakeClient.error = aiohttp.ServerTimeoutError("probe-secret")
    result = await netbox_probe.probe_netbox_endpoint(_endpoint())
    client = _FakeClient.created[0]

    assert result.reachable is False
    assert result.status == "timeout"
    assert result.error_type == "ServerTimeoutError"
    assert result.error == "ServerTimeoutError: [REDACTED]"
    with pytest.raises(ProxboxException) as raised:
        netbox_probe.reject_recent_unreachable(_FakeApi(client))
    assert raised.value.http_status_code == 504
    assert "probe-secret" not in repr(result)
    assert "probe-secret" not in repr(raised.value.detail)


def test_expired_or_different_configuration_does_not_fail_fast(monkeypatch) -> None:
    config = Config(base_url="https://netbox.example.test", token_secret="one")
    result = netbox_probe.NetBoxProbeResult(reachable=False, status="connection_error")
    netbox_probe._store(config, result)
    monkeypatch.setattr(netbox_probe.time, "time", lambda: 10_000.0)

    api = _FakeApi(_FakeClient(Config(base_url="https://other.example.test", token_secret="two")))
    netbox_probe.reject_recent_unreachable(api)

    monkeypatch.setattr(netbox_probe, "_read_recent_probe", lambda _config: 1 / 0)
    netbox_probe.reject_recent_unreachable(api)


@pytest.mark.parametrize("created", [10_001.0, math.nan, math.inf, -math.inf])
def test_invalid_or_future_timestamp_is_an_advisory_miss(monkeypatch, created: float) -> None:
    config = Config(base_url="https://netbox.example.test", token_secret="one")
    path = netbox_probe._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    netbox_probe._write_cache(
        path,
        {
            netbox_probe._fingerprint(config): {
                "created": created,
                "result": {"reachable": False, "status": "timeout"},
            }
        },
    )
    monkeypatch.setattr(netbox_probe.time, "time", lambda: 10_000.0)
    assert netbox_probe.recent_probe(config) is None


def test_unsafe_or_oversized_cache_files_are_advisory_misses(tmp_path) -> None:
    config = Config(base_url="https://netbox.example.test", token_secret="one")
    path = netbox_probe._cache_path()
    path.write_bytes(b"x" * (netbox_probe._MAX_CACHE_BYTES + 1))
    assert netbox_probe.recent_probe(config) is None

    path.unlink()
    os.mkfifo(path)
    assert netbox_probe.recent_probe(config) is None

    path.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    assert netbox_probe.recent_probe(config) is None


def test_fingerprint_is_unambiguous_for_delimiter_bearing_credentials() -> None:
    first = Config(base_url="https://netbox", token_key="a|b", token_secret="c")
    second = Config(base_url="https://netbox", token_key="a", token_secret="b|c")
    assert netbox_probe._fingerprint(first) != netbox_probe._fingerprint(second)


def test_probe_cache_is_visible_to_another_worker_process() -> None:
    config = Config(base_url="https://netbox.example.test", token_secret="shared")
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    ready = context.Event()
    start = context.Event()
    worker = context.Process(
        target=_read_probe_in_child,
        args=(queue, ready, start, str(netbox_probe._cache_path())),
    )
    worker.start()
    assert ready.wait(timeout=120)
    netbox_probe._store(
        config,
        netbox_probe.NetBoxProbeResult(reachable=False, status="connection_error"),
    )
    start.set()
    worker.join(timeout=10)

    assert worker.exitcode == 0
    assert queue.get(timeout=1)["status"] == "connection_error"
    queue.close()


@pytest.mark.asyncio
async def test_sync_tag_dependency_fails_before_netbox_request(monkeypatch) -> None:
    config = Config(base_url="https://netbox.example.test", token_secret="secret")
    netbox_probe._store(
        config,
        netbox_probe.NetBoxProbeResult(reachable=False, status="connection_error"),
    )
    ensure = AsyncMock()
    monkeypatch.setattr("proxbox_api.dependencies.ensure_tag_async", ensure)

    with pytest.raises(ProxboxException) as raised:
        await proxbox_tag(_FakeApi(_FakeClient(config)))

    assert raised.value.http_status_code == 502
    ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_bootstrap_dependency_fails_before_netbox_request(monkeypatch) -> None:
    config = Config(base_url="https://netbox.example.test", token_secret="secret")
    netbox_probe._store(
        config,
        netbox_probe.NetBoxProbeResult(reachable=False, status="timeout"),
    )
    bootstrap = AsyncMock()
    monkeypatch.setattr("proxbox_api.dependencies.run_netbox_bootstrap", bootstrap)

    with pytest.raises(ProxboxException) as raised:
        await ensure_netbox_sync_dependencies(_FakeApi(_FakeClient(config)))

    assert raised.value.http_status_code == 504
    bootstrap.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_propagates_cancellation_and_still_closes() -> None:
    _FakeClient.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await netbox_probe.probe_netbox_endpoint(_endpoint())
    assert _FakeClient.created[0].closed == 1


@pytest.mark.asyncio
async def test_probe_close_finishes_through_repeated_cancellation(monkeypatch) -> None:
    close_started = asyncio.Event()
    permit_close = asyncio.Event()

    class _BlockingCloseClient(_FakeClient):
        async def close(self) -> None:
            close_started.set()
            await permit_close.wait()
            await super().close()

    _BlockingCloseClient.error = asyncio.CancelledError()
    monkeypatch.setattr(netbox_probe, "NetBoxApiClient", _BlockingCloseClient)
    task = asyncio.create_task(netbox_probe.probe_netbox_endpoint(_endpoint()))
    await close_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    permit_close.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert _BlockingCloseClient.created[-1].closed == 1


@pytest.mark.asyncio
async def test_probe_does_not_wait_forever_for_stalled_close(monkeypatch) -> None:
    close_cancelled = asyncio.Event()

    class _StalledCloseClient(_FakeClient):
        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                close_cancelled.set()

    monkeypatch.setattr(netbox_probe, "NetBoxApiClient", _StalledCloseClient)
    monkeypatch.setattr(netbox_probe, "PROBE_CLOSE_TIMEOUT_SECONDS", 0.01)

    result = await asyncio.wait_for(netbox_probe.probe_netbox_endpoint(_endpoint()), timeout=0.2)
    await asyncio.wait_for(close_cancelled.wait(), timeout=0.2)

    assert result.reachable is True
