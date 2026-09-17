"""Short, credential-safe NetBox reachability probes and recent-result cache."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from netbox_sdk.client import NetBoxApiClient
from netbox_sdk.config import Config
from netbox_sdk.facade import Api
from netbox_sdk.schema import build_schema_index
from pydantic import BaseModel

from proxbox_api.constants import NETBOX_SCHEMA_VERSION
from proxbox_api.database import NetBoxEndpoint, resolve_database_target
from proxbox_api.exception import ProxboxException
from proxbox_api.session.netbox import netbox_config_from_endpoint
from proxbox_api.utils.async_compat import maybe_await
from proxbox_api.utils.retry import (
    describe_exception,
    is_netbox_connection_error,
    is_netbox_timeout_error,
)

PROBE_TIMEOUT_SECONDS = 10.0
PROBE_CLOSE_TIMEOUT_SECONDS = 1.0
PROBE_CACHE_TTL_SECONDS = 30.0
_MAX_CACHE_ENTRIES = 128
_MAX_CACHE_BYTES = 131_072


class NetBoxProbeResult(BaseModel):
    reachable: bool
    status: str
    api_version: str | None = None
    error_type: str | None = None
    error: str | None = None
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS


_CACHE_FILENAME = "netbox-probe-cache.json"


def _fingerprint(config: Config) -> str:
    material = json.dumps(
        [
            config.base_url or "",
            config.token_version or "",
            config.token_key or "",
            config.token_secret or "",
            bool(config.ssl_verify),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _cache_path() -> Path:
    return resolve_database_target().path.parent / _CACHE_FILENAME


@contextmanager
def _locked_cache() -> Any:
    path = _cache_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path.with_suffix(".lock"),
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield path
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _valid_cache_metadata(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and not metadata.st_mode & 0o077
        and metadata.st_size <= _MAX_CACHE_BYTES
    )


def _decode_cache(data: bytes) -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(data)
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _read_cache(path: Path) -> dict[str, dict[str, object]]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not _valid_cache_metadata(metadata):
            return {}
        data = os.read(descriptor, _MAX_CACHE_BYTES + 1)
        if len(data) > _MAX_CACHE_BYTES:
            return {}
        return _decode_cache(data)
    except (FileNotFoundError, OSError):
        return {}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_cache(path: Path, entries: dict[str, dict[str, object]]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(entries, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _version(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    values = cast("dict[str, object]", payload)
    for key in ("netbox-version", "netbox_version", "version"):
        value = values.get(key)
        if value is not None:
            return str(value)
    return None


def _safe_error(error: BaseException, config: Config) -> str:
    description = describe_exception(error)
    for secret in (config.token_key, config.token_secret):
        if secret:
            description = description.replace(secret, "[REDACTED]")
    return description


def _failure(error: BaseException, config: Config) -> NetBoxProbeResult:
    if is_netbox_timeout_error(error):
        status = "timeout"
    elif is_netbox_connection_error(error):
        status = "connection_error"
    else:
        status = "error"
    return NetBoxProbeResult(
        reachable=False,
        status=status,
        error_type=type(error).__name__,
        error=_safe_error(error, config),
    )


def _created(entry: dict[str, object]) -> float:
    value = entry.get("created", 0)
    if not isinstance(value, (int, float, str)):
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _is_fresh(created: float, now: float) -> bool:
    return math.isfinite(created) and created <= now and now - created <= PROBE_CACHE_TTL_SECONDS


def _consume_close_result(task: asyncio.Task[object]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _close_client_durably(client: NetBoxApiClient) -> None:
    close_task = asyncio.create_task(maybe_await(client.close()))
    deadline = asyncio.get_running_loop().time() + PROBE_CLOSE_TIMEOUT_SECONDS
    cancelled = False
    while not close_task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({close_task}, timeout=remaining)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    if close_task.done():
        _consume_close_result(close_task)
    else:
        close_task.cancel()
        close_task.add_done_callback(_consume_close_result)
    if cancelled:
        raise asyncio.CancelledError


def _store(config: Config, result: NetBoxProbeResult) -> None:
    now = time.time()
    with _locked_cache() as path:
        cache = _read_cache(path)
        expired = [key for key, value in cache.items() if not _is_fresh(_created(value), now)]
        for key in expired:
            cache.pop(key, None)
        if len(cache) >= _MAX_CACHE_ENTRIES:
            oldest = min(cache, key=lambda key: _created(cache[key]))
            cache.pop(oldest, None)
        cache[_fingerprint(config)] = {"created": now, "result": result.model_dump()}
        _write_cache(path, cache)


def _read_recent_probe(config: Config) -> NetBoxProbeResult | None:
    key = _fingerprint(config)
    with _locked_cache() as path:
        cache = _read_cache(path)
        cached = cache.get(key)
        if cached is None:
            return None
        created = _created(cached)
        if not _is_fresh(created, time.time()):
            cache.pop(key, None)
            _write_cache(path, cache)
            return None
        try:
            return NetBoxProbeResult.model_validate(cached.get("result"))
        except (ValueError, TypeError):
            cache.pop(key, None)
            _write_cache(path, cache)
            return None


def recent_probe(config: Config) -> NetBoxProbeResult | None:
    """Return a fresh shared result; cache I/O failure behaves as an advisory miss."""
    try:
        return _read_recent_probe(config)
    except Exception:
        return None


async def probe_netbox_endpoint(endpoint: NetBoxEndpoint) -> NetBoxProbeResult:
    """Probe `/api/status/` with a request-private client and bounded timeout."""
    config: Config | None = None
    api: Api | None = None
    try:
        config = netbox_config_from_endpoint(endpoint)
        config.timeout = PROBE_TIMEOUT_SECONDS
        api = Api(
            client=NetBoxApiClient(config),
            schema=build_schema_index(version=NETBOX_SCHEMA_VERSION),
        )
        payload: Any = await asyncio.wait_for(api.status(), timeout=PROBE_TIMEOUT_SECONDS)
        result = NetBoxProbeResult(
            reachable=True,
            status="reachable",
            api_version=_version(payload),
        )
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        result = (
            _failure(error, config)
            if config is not None
            else NetBoxProbeResult(
                reachable=False,
                status="error",
                error_type=type(error).__name__,
                error="Probe configuration failed",
            )
        )
    finally:
        if api is not None:
            await _close_client_durably(api.client)
    if config is not None:
        try:
            _store(config, result)
        except Exception:
            pass
    return result


def reject_recent_unreachable(api: Api) -> None:
    """Fail fast only when a fresh probe covers this exact client configuration."""
    client = getattr(api, "client", None)
    config = getattr(client, "config", None)
    if not isinstance(config, Config):
        return
    result = recent_probe(config)
    if result is None or result.reachable:
        return
    status_code = 504 if result.status == "timeout" else 502
    raise ProxboxException(
        message="NetBox endpoint is not reachable",
        detail={
            "cause": result.error_type or result.status,
            "error": result.error or result.status,
            "hint": "Check that the configured NetBox URL is reachable from proxbox-api.",
            "probe_timeout_seconds": result.timeout_seconds,
        },
        http_status_code=status_code,
    )


def clear_probe_cache() -> None:
    with _locked_cache() as path:
        _write_cache(path, {})
