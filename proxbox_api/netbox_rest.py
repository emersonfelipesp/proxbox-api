"""Direct REST helpers for NetBox resources that bypass schema-bound facade traversal."""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Any
from urllib.parse import urlsplit

from netbox_sdk.client import ApiResponse
from pydantic import BaseModel

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_sdk_helpers import to_dict
from proxbox_api.schemas.netbox.extras import TagSchema
from proxbox_api.utils.retry import (
    _is_connection_refused_error,
    _is_netbox_overwhelmed_error,
    _is_transient_netbox_error,
)


def _resolve_netbox_max_concurrent() -> int:
    """Resolve max concurrent NetBox requests from environment."""
    raw = os.environ.get("PROXBOX_NETBOX_MAX_CONCURRENT", "").strip()
    if not raw:
        # Default to 1 to avoid exhausting NetBox DB connection pools.
        # Increase only if NetBox has sufficient PostgreSQL pool capacity.
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


_netbox_request_semaphore: asyncio.Semaphore | None = None
_netbox_request_semaphore_loop_id: int | None = None
_netbox_get_cache: dict[tuple[int, str, str], tuple[float, int, list[dict[str, Any]]]] = {}

_cache_metrics_hits: int = 0
_cache_metrics_misses: int = 0
_cache_metrics_invalidations: int = 0
_cache_metrics_evictions_ttl: int = 0
_cache_metrics_evictions_size: int = 0
_cache_metrics_evictions_bytes: int = 0


def _reset_netbox_globals() -> None:
    """Reset all module-level state. Call between tests to prevent cache and semaphore leaks."""
    global _netbox_request_semaphore, _netbox_request_semaphore_loop_id
    global _cache_metrics_hits, _cache_metrics_misses, _cache_metrics_invalidations
    global \
        _cache_metrics_evictions_ttl, \
        _cache_metrics_evictions_size, \
        _cache_metrics_evictions_bytes
    _netbox_request_semaphore = None
    _netbox_request_semaphore_loop_id = None
    _netbox_get_cache.clear()
    _cache_metrics_hits = 0
    _cache_metrics_misses = 0
    _cache_metrics_invalidations = 0
    _cache_metrics_evictions_ttl = 0
    _cache_metrics_evictions_size = 0
    _cache_metrics_evictions_bytes = 0


def get_cache_metrics() -> dict[str, Any]:
    """Return current cache metrics for observability."""
    global _cache_metrics_hits, _cache_metrics_misses, _cache_metrics_invalidations
    global \
        _cache_metrics_evictions_ttl, \
        _cache_metrics_evictions_size, \
        _cache_metrics_evictions_bytes

    ttl = _resolve_get_cache_ttl_seconds()
    max_entries = _resolve_get_cache_max_entries()
    max_bytes = _resolve_get_cache_max_bytes()
    now = time.monotonic()

    oldest = 0.0
    current_bytes = 0
    if _netbox_get_cache:
        oldest = min(now - cached_at for cached_at, _, _ in _netbox_get_cache.values())
        current_bytes = sum(size_bytes for _, size_bytes, _ in _netbox_get_cache.values())

    return {
        "hits": _cache_metrics_hits,
        "misses": _cache_metrics_misses,
        "hit_rate": (
            round(_cache_metrics_hits / (_cache_metrics_hits + _cache_metrics_misses) * 100, 2)
            if (_cache_metrics_hits + _cache_metrics_misses) > 0
            else 0.0
        ),
        "invalidations": _cache_metrics_invalidations,
        "evictions_ttl": _cache_metrics_evictions_ttl,
        "evictions_size": _cache_metrics_evictions_size,
        "evictions_bytes": _cache_metrics_evictions_bytes,
        "current_entries": len(_netbox_get_cache),
        "current_bytes": current_bytes,
        "max_entries": max_entries,
        "max_bytes": max_bytes,
        "ttl_seconds": ttl,
        "oldest_entry_age_seconds": round(oldest, 2),
    }


def get_cache_prometheus_metrics() -> str:
    """Return cache metrics in Prometheus exposition format."""
    metrics = get_cache_metrics()
    lines = [
        "# HELP proxbox_cache_hits Total number of cache hits",
        "# TYPE proxbox_cache_hits counter",
        f"proxbox_cache_hits {metrics['hits']}",
        "# HELP proxbox_cache_misses Total number of cache misses",
        "# TYPE proxbox_cache_misses counter",
        f"proxbox_cache_misses {metrics['misses']}",
        "# HELP proxbox_cache_hit_rate Cache hit rate percentage",
        "# TYPE proxbox_cache_hit_rate gauge",
        f"proxbox_cache_hit_rate {metrics['hit_rate']}",
        "# HELP proxbox_cache_invalidations Total number of cache invalidations",
        "# TYPE proxbox_cache_invalidations counter",
        f"proxbox_cache_invalidations {metrics['invalidations']}",
        "# HELP proxbox_cache_evictions_ttl Total entries evicted due to TTL expiry",
        "# TYPE proxbox_cache_evictions_ttl counter",
        f"proxbox_cache_evictions_ttl {metrics['evictions_ttl']}",
        "# HELP proxbox_cache_evictions_size Total entries evicted due to entry count limit",
        "# TYPE proxbox_cache_evictions_size counter",
        f"proxbox_cache_evictions_size {metrics['evictions_size']}",
        "# HELP proxbox_cache_evictions_bytes Total bytes evicted due to size limit",
        "# TYPE proxbox_cache_evictions_bytes counter",
        f"proxbox_cache_evictions_bytes {metrics['evictions_bytes']}",
        "# HELP proxbox_cache_entries Current number of cached entries",
        "# TYPE proxbox_cache_entries gauge",
        f"proxbox_cache_entries {metrics['current_entries']}",
        "# HELP proxbox_cache_bytes Current cache size in bytes",
        "# TYPE proxbox_cache_bytes gauge",
        f"proxbox_cache_bytes {metrics['current_bytes']}",
        "# HELP proxbox_cache_max_entries Maximum cache entry count",
        "# TYPE proxbox_cache_max_entries gauge",
        f"proxbox_cache_max_entries {metrics['max_entries']}",
        "# HELP proxbox_cache_max_bytes Maximum cache size in bytes",
        "# TYPE proxbox_cache_max_bytes gauge",
        f"proxbox_cache_max_bytes {metrics['max_bytes']}",
        "# HELP proxbox_cache_ttl_seconds Cache TTL setting",
        "# TYPE proxbox_cache_ttl_seconds gauge",
        f"proxbox_cache_ttl_seconds {metrics['ttl_seconds']}",
        "# HELP proxbox_cache_oldest_age_seconds Age of oldest entry in cache",
        "# TYPE proxbox_cache_oldest_age_seconds gauge",
        f"proxbox_cache_oldest_age_seconds {metrics['oldest_entry_age_seconds']}",
    ]
    return "\n".join(lines) + "\n"


def _debug_cache_enabled() -> bool:
    """Check if debug cache logging is enabled."""
    return os.environ.get("PROXBOX_DEBUG_CACHE", "0").strip() == "1"


def _resolve_get_cache_ttl_seconds() -> float:
    """Resolve NetBox GET cache TTL from environment."""
    raw = os.environ.get("PROXBOX_NETBOX_GET_CACHE_TTL", "").strip()
    if not raw:
        return 60.0
    try:
        ttl = float(raw)
    except ValueError:
        return 60.0
    return max(0.0, ttl)


def _resolve_get_cache_max_entries() -> int:
    """Resolve NetBox GET cache max entries from environment."""
    raw = os.environ.get("PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES", "").strip()
    if not raw:
        return 4096
    try:
        return max(1, int(raw))
    except ValueError:
        return 4096


def _resolve_get_cache_max_bytes() -> int:
    """Resolve NetBox GET cache max bytes from environment."""
    raw = os.environ.get("PROXBOX_NETBOX_GET_CACHE_MAX_BYTES", "").strip()
    if not raw:
        return 50 * 1024 * 1024  # 50 MB default
    try:
        return max(1024, int(raw))
    except ValueError:
        return 50 * 1024 * 1024


def _calculate_cache_entry_size(records: list[dict[str, Any]]) -> int:
    """Calculate approximate memory size of cache entry in bytes."""
    try:
        return len(json.dumps(records, default=str))
    except TypeError:
        return len(str(records))


def _serialize_query(query: dict[str, object] | None) -> str:
    if not query:
        return ""
    try:
        return json.dumps(query, sort_keys=True, default=str, separators=(",", ":"))
    except TypeError:
        # Fallback for non-JSON values while still keeping deterministic ordering.
        normalized = {key: str(value) for key, value in sorted(query.items())}
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _cache_key(api: object, path: str, query: dict[str, object] | None) -> tuple[int, str, str]:
    return (id(api), _normalize_path(path), _serialize_query(query))


def clear_rest_get_cache() -> None:
    """Clear the in-memory NetBox GET response cache."""
    global _cache_metrics_hits, _cache_metrics_misses, _cache_metrics_invalidations
    global \
        _cache_metrics_evictions_ttl, \
        _cache_metrics_evictions_size, \
        _cache_metrics_evictions_bytes
    _netbox_get_cache.clear()
    _cache_metrics_hits = 0
    _cache_metrics_misses = 0
    _cache_metrics_invalidations = 0
    _cache_metrics_evictions_ttl = 0
    _cache_metrics_evictions_size = 0
    _cache_metrics_evictions_bytes = 0


def _prune_get_cache(now: float, counting: bool = True) -> None:
    global \
        _cache_metrics_evictions_ttl, \
        _cache_metrics_evictions_size, \
        _cache_metrics_evictions_bytes
    ttl = _resolve_get_cache_ttl_seconds()
    if ttl <= 0:
        _netbox_get_cache.clear()
        return

    expired_keys = [
        key for key, (cached_at, _, _) in _netbox_get_cache.items() if (now - cached_at) >= ttl
    ]
    for key in expired_keys:
        _netbox_get_cache.pop(key, None)

    max_entries = _resolve_get_cache_max_entries()
    max_bytes = _resolve_get_cache_max_bytes()

    current_bytes = sum(size_bytes for _, size_bytes, _ in _netbox_get_cache.values())
    entries_to_evict = max(0, len(_netbox_get_cache) - max_entries)
    bytes_to_evict = max(0, current_bytes - max_bytes)

    if entries_to_evict > 0 or bytes_to_evict > 0:
        sorted_entries = sorted(_netbox_get_cache.items(), key=lambda item: item[1][0])
        evicted_entries = 0
        evicted_bytes = 0

        for key, (_, size_bytes, _) in sorted_entries:
            if entries_to_evict > 0 and evicted_entries < entries_to_evict:
                _netbox_get_cache.pop(key, None)
                evicted_entries += 1
                if counting:
                    _cache_metrics_evictions_size += 1
            elif bytes_to_evict > 0 and evicted_bytes < bytes_to_evict:
                _netbox_get_cache.pop(key, None)
                evicted_bytes += size_bytes
                if counting:
                    _cache_metrics_evictions_size += 1
                    _cache_metrics_evictions_bytes += size_bytes
            else:
                break

        if counting and bytes_to_evict > evicted_bytes:
            _cache_metrics_evictions_bytes += evicted_bytes


def _read_get_cache(
    api: object,
    path: str,
    query: dict[str, object] | None,
) -> list[dict[str, object]] | None:
    global _cache_metrics_hits, _cache_metrics_misses
    now = time.monotonic()
    _prune_get_cache(now, counting=False)

    ttl = _resolve_get_cache_ttl_seconds()
    if ttl <= 0:
        if _debug_cache_enabled():
            logger.debug("Cache DISABLED: TTL=%s path=%s query=%s", ttl, path, query)
        return None

    cache_key = _cache_key(api, path, query)
    entry = _netbox_get_cache.get(cache_key)
    if entry is None:
        _cache_metrics_misses += 1
        if _debug_cache_enabled():
            logger.debug("Cache MISS: path=%s query=%s", path, query)
        return None
    cached_at, size_bytes, records = entry
    if (now - cached_at) >= ttl:
        _netbox_get_cache.pop(cache_key, None)
        _cache_metrics_misses += 1
        if _debug_cache_enabled():
            logger.debug("Cache EXPIRED: path=%s query=%s age=%s", path, query, now - cached_at)
        return None
    _cache_metrics_hits += 1
    if _debug_cache_enabled():
        logger.debug("Cache HIT: path=%s query=%s", path, query)
    return [dict(record) for record in records]


def _write_get_cache(
    api: object,
    path: str,
    query: dict[str, object] | None,
    records: list[dict[str, object]],
) -> None:
    global \
        _cache_metrics_evictions_ttl, \
        _cache_metrics_evictions_size, \
        _cache_metrics_evictions_bytes
    now = time.monotonic()
    ttl = _resolve_get_cache_ttl_seconds()
    if ttl <= 0:
        return

    entry_size = _calculate_cache_entry_size(records)
    max_bytes = _resolve_get_cache_max_bytes()

    expired_keys = [
        key for key, (cached_at, _, _) in _netbox_get_cache.items() if (now - cached_at) >= ttl
    ]
    for key in expired_keys:
        _netbox_get_cache.pop(key, None)
    if expired_keys:
        _cache_metrics_evictions_ttl += len(expired_keys)

    max_entries = _resolve_get_cache_max_entries()
    current_bytes = sum(size_bytes for _, size_bytes, _ in _netbox_get_cache.values())

    while len(_netbox_get_cache) >= max_entries or (current_bytes + entry_size) > max_bytes:
        if not _netbox_get_cache:
            break
        oldest_key = min(_netbox_get_cache.items(), key=lambda item: item[1][0])[0]
        evicted_size = _netbox_get_cache.pop(oldest_key, (0, 0, []))[1]
        current_bytes -= evicted_size
        _cache_metrics_evictions_size += 1
        _cache_metrics_evictions_bytes += evicted_size

    _netbox_get_cache[_cache_key(api, path, query)] = (
        now,
        entry_size,
        [dict(record) for record in records],
    )


def _is_detail_path(path: str) -> bool:
    """Check if a path is a detail endpoint (ends with numeric ID)."""
    segments = path.strip("/").split("/")
    return len(segments) > 0 and segments[-1].isdigit()


def _extract_list_path(detail_path: str) -> str:
    """Extract list path from a detail path."""
    normalized = _normalize_path(detail_path)
    segments = normalized.strip("/").split("/")
    if segments and segments[-1].isdigit():
        return "/" + "/".join(segments[:-1]) + "/"
    return normalized


def _invalidate_get_cache_for_path(api: object, path: str) -> None:
    """Invalidate cache entries for an exact path, its parent list, and direct children."""
    global _cache_metrics_invalidations
    normalized = _normalize_path(path)
    api_id = id(api)
    is_detail = _is_detail_path(normalized)
    list_path = _extract_list_path(normalized) if is_detail else normalized

    to_remove = []
    for key in _netbox_get_cache:
        if key[0] != api_id:
            continue
        cached_path = key[1]

        if cached_path == normalized:
            to_remove.append(key)
            continue

        if is_detail and cached_path == list_path:
            to_remove.append(key)
            continue

        if not is_detail and cached_path.startswith(normalized):
            remainder = cached_path[len(normalized) :].strip("/")
            if remainder and "/" not in remainder and remainder.isdigit():
                to_remove.append(key)

    for key in to_remove:
        _netbox_get_cache.pop(key, None)
    _cache_metrics_invalidations += len(to_remove)

    if _debug_cache_enabled():
        logger.debug("Cache INVALIDATE: %d entries for path=%s", len(to_remove), path)


def _invalidate_get_cache_for_record(
    api: object, list_path: str, record: dict[str, object]
) -> None:
    """Invalidate cache for list endpoint, detail endpoint from URL, and detail endpoint from ID."""
    _invalidate_get_cache_for_path(api, list_path)
    record_url = record.get("url")
    if isinstance(record_url, str):
        parsed = urlsplit(record_url)
        if parsed.path:
            _invalidate_get_cache_for_path(api, parsed.path)
    record_id = record.get("id")
    if record_id is not None:
        _invalidate_get_cache_for_path(api, _detail_path(list_path, record_id))


def _get_netbox_semaphore() -> asyncio.Semaphore:
    """Get or create the global NetBox request semaphore."""
    global _netbox_request_semaphore, _netbox_request_semaphore_loop_id

    current_loop = asyncio.get_running_loop()
    current_loop_id = id(current_loop)
    if _netbox_request_semaphore is None or _netbox_request_semaphore_loop_id != current_loop_id:
        _netbox_request_semaphore = asyncio.Semaphore(_resolve_netbox_max_concurrent())
        _netbox_request_semaphore_loop_id = current_loop_id
    return _netbox_request_semaphore


def _unwrap_api(nb: object) -> object:
    return nb


def _normalize_path(path: str) -> str:
    normalized = path.strip()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return normalized


def _detail_path(list_path: str, record_id: object) -> str:
    list_path = _normalize_path(list_path)
    return f"{list_path}{record_id}/"


def _extract_payload(response: ApiResponse) -> object:
    if response.status < 200 or response.status >= 300:
        detail = response.text
        try:
            payload = response.json()
        except json.JSONDecodeError:
            logger.debug(
                "NetBox error response body was not JSON (status=%s)",
                response.status,
                exc_info=True,
            )
            payload = None
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or detail)
        raise ProxboxException(
            message="NetBox REST request failed",
            detail=detail,
        )
    return response.json()


def _handle_netbox_error(error: Exception, operation: str) -> None:
    """Log and re-raise NetBox errors with better context."""
    error_str = str(error)
    is_transient = _is_transient_netbox_error(error)

    if is_transient:
        logger.warning(
            "Transient NetBox error during %s: %s",
            operation,
            error_str,
        )
    else:
        logger.error(
            "NetBox error during %s: %s",
            operation,
            error_str,
        )

    if isinstance(error, ProxboxException):
        raise

    if _is_netbox_overwhelmed_error(error):
        raise ProxboxException(
            message="NetBox is overwhelmed. Please retry in a few moments.",
            detail=f"{operation} failed: {error_str}",
            python_exception=error_str,
        ) from error

    raise ProxboxException(
        message=f"NetBox {operation} failed",
        detail=error_str,
        python_exception=error_str,
    ) from error


def _compute_retry_delay(base_delay: float, attempt: int, error: Exception) -> float:
    """Compute backoff delay with stronger throttling when NetBox is overloaded."""
    exponential_delay = base_delay * (2**attempt)
    if _is_connection_refused_error(error):
        exponential_delay = max(exponential_delay, 10.0)
    if _is_netbox_overwhelmed_error(error):
        # Aggressive backoff when DB pool is saturated - wait up to 30s
        exponential_delay = max(exponential_delay, 30.0)
    return exponential_delay + random.uniform(0, exponential_delay * 0.5)


def _is_duplicate_error(detail: object) -> bool:
    if isinstance(detail, dict):
        return any(_is_duplicate_error(value) for value in detail.values())
    if isinstance(detail, list):
        return any(_is_duplicate_error(value) for value in detail)
    text = str(detail).lower()
    return "already exists" in text or "must be unique" in text


def _candidate_reuse_lookups(
    lookup: dict[str, object],
    payload: dict[str, object],
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    seen: set[tuple[tuple[str, object], ...]] = set()

    def _add(candidate: dict[str, object]) -> None:
        normalized = {key: value for key, value in candidate.items() if value not in (None, "")}
        if not normalized:
            return
        key = tuple(sorted(normalized.items()))
        if key in seen:
            return
        seen.add(key)
        candidates.append(normalized)

    _add(lookup)

    for field in ("slug", "name", "model"):
        value = payload.get(field)
        if value not in (None, ""):
            _add({field: value})

    if payload.get("name") not in (None, "") and payload.get("site") not in (None, ""):
        _add({"name": payload["name"], "site_id": payload["site"]})

    if payload.get("manufacturer") not in (None, "") and payload.get("model") not in (None, ""):
        _add({"manufacturer_id": payload["manufacturer"], "model": payload["model"]})

    return candidates


class RestRecord:
    """Minimal mutable record wrapper for direct NetBox REST resources."""

    def __init__(self, api: object, list_path: str, values: dict[str, object]) -> None:
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_list_path", _normalize_path(list_path))
        object.__setattr__(self, "_data", dict(values))
        object.__setattr__(self, "_dirty_fields", set())

    @property
    def id(self) -> object:
        return self._data.get("id")

    @property
    def url(self) -> str | None:
        value = self._data.get("url")
        return value if isinstance(value, str) else None

    @property
    def _detail_path(self) -> str:
        if self.url:
            parsed = urlsplit(self.url)
            if parsed.path:
                return _normalize_path(parsed.path)
        if self.id is None:
            raise ProxboxException(message="Cannot resolve detail path for unsaved NetBox record")
        return _detail_path(self._list_path, self.id)

    def serialize(self) -> dict[str, object]:
        return dict(self._data)

    def dict(self) -> dict[str, object]:
        return self.serialize()

    @property
    def json(self) -> dict[str, object]:
        return self.serialize()

    def get(self, key: str, default: object = None) -> object:
        return self._data.get(key, default)

    def __getattr__(self, name: str) -> object:
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"_api", "_list_path", "_data", "_dirty_fields"}:
            object.__setattr__(self, name, value)
        else:
            self._data[name] = value
            self._dirty_fields.add(name)

    async def save(self) -> RestRecord:
        payload = {
            field: self._data[field]
            for field in object.__getattribute__(self, "_dirty_fields")
            if field in self._data
        }
        if not payload:
            return self
        try:
            response = await self._api.client.request(
                "PATCH",
                self._detail_path,
                payload=payload,
            )
        except Exception as e:
            _handle_netbox_error(e, f"save record {self._detail_path}")
            raise  # Early return via exception

        try:
            response_payload = _extract_payload(response)
        except ProxboxException:
            raise
        except Exception as e:
            _handle_netbox_error(e, f"parse save record {self._detail_path}")
            raise  # Early return via exception

        if not isinstance(response_payload, dict):
            raise ProxboxException(message="NetBox returned invalid JSON for record update")
        _invalidate_get_cache_for_record(
            self._api,
            self._list_path,
            response_payload,
        )
        object.__setattr__(self, "_data", response_payload)
        object.__setattr__(self, "_dirty_fields", set())
        return self

    async def delete(self) -> bool:
        try:
            response = await self._api.client.request(
                "DELETE", self._detail_path, expect_json=False
            )
        except Exception as e:
            _handle_netbox_error(e, f"delete record {self._detail_path}")
            raise  # Early return via exception

        if response.status not in {200, 204}:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail=response.text,
            )
        _invalidate_get_cache_for_path(self._api, self._list_path)
        _invalidate_get_cache_for_path(self._api, self._detail_path)
        return True


async def rest_list_async(
    nb: object, path: str, *, query: dict[str, object] | None = None
) -> list[RestRecord]:
    api = _unwrap_api(nb)
    semaphore = _get_netbox_semaphore()
    normalized_path = _normalize_path(path)

    cached = _read_get_cache(api, normalized_path, query)
    if cached is not None:
        return [RestRecord(api, normalized_path, item) for item in cached]

    async def _do_request() -> list[RestRecord]:
        try:
            response = await api.client.request("GET", normalized_path, query=query)
        except Exception as e:
            _handle_netbox_error(e, f"list {path}")
            raise  # Early return via exception

        try:
            payload = _extract_payload(response)
        except ProxboxException:
            raise
        except Exception as e:
            _handle_netbox_error(e, f"parse list {path}")
            raise  # Early return via exception

        if isinstance(payload, dict):
            results = payload.get("results", [])
        elif isinstance(payload, list):
            results = payload
        else:
            raise ProxboxException(message="NetBox REST list response was not JSON array/object")
        if not isinstance(results, list):
            raise ProxboxException(message="NetBox REST list response missing results list")
        normalized_results = [item if isinstance(item, dict) else to_dict(item) for item in results]
        _write_get_cache(api, normalized_path, query, normalized_results)
        return [RestRecord(api, normalized_path, item) for item in normalized_results]

    # Retry loop with semaphore and exponential backoff for transient errors
    max_retries = max(0, int(os.environ.get("PROXBOX_NETBOX_MAX_RETRIES", "5")))
    base_delay = float(os.environ.get("PROXBOX_NETBOX_RETRY_DELAY", "2.0"))

    for attempt in range(max_retries + 1):
        async with semaphore:
            try:
                return await _do_request()
            except Exception as e:
                if attempt == max_retries or not _is_transient_netbox_error(e):
                    raise
                delay = _compute_retry_delay(base_delay, attempt, e)
                pressure_note = " (NetBox overwhelmed)" if _is_netbox_overwhelmed_error(e) else ""
                logger.warning(
                    "NetBox request failed%s (attempt %s/%s), retrying in %ss: %s",
                    pressure_note,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    str(e)[:200],
                )
        await asyncio.sleep(delay)

    # Should not reach here, but satisfy type checker
    return await _do_request()


async def rest_first_async(
    nb: object,
    path: str,
    *,
    query: dict[str, object] | None = None,
) -> RestRecord | None:
    records = await rest_list_async(nb, path, query=query)
    if not records:
        return None
    return records[0]


async def rest_create_async(nb: object, path: str, payload: dict[str, object]) -> RestRecord:
    api = _unwrap_api(nb)
    semaphore = _get_netbox_semaphore()
    normalized_path = _normalize_path(path)

    async def _do_request() -> RestRecord:
        try:
            response = await api.client.request("POST", normalized_path, payload=payload)
        except Exception as e:
            _handle_netbox_error(e, f"create {path}")
            raise  # Early return via exception

        try:
            body = _extract_payload(response)
        except ProxboxException:
            raise
        except Exception as e:
            _handle_netbox_error(e, f"parse create {path}")
            raise  # Early return via exception

        if not isinstance(body, dict):
            raise ProxboxException(message="NetBox REST create response was not a JSON object")
        _invalidate_get_cache_for_record(api, normalized_path, body)
        return RestRecord(api, normalized_path, body)

    # Retry loop with semaphore and exponential backoff for transient errors
    max_retries = max(0, int(os.environ.get("PROXBOX_NETBOX_MAX_RETRIES", "5")))
    base_delay = float(os.environ.get("PROXBOX_NETBOX_RETRY_DELAY", "2.0"))

    for attempt in range(max_retries + 1):
        async with semaphore:
            try:
                return await _do_request()
            except Exception as e:
                if attempt == max_retries or not _is_transient_netbox_error(e):
                    raise
                delay = _compute_retry_delay(base_delay, attempt, e)
                pressure_note = " (NetBox overwhelmed)" if _is_netbox_overwhelmed_error(e) else ""
                logger.warning(
                    "NetBox create failed%s (attempt %s/%s), retrying in %ss: %s",
                    pressure_note,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    str(e)[:200],
                )
        await asyncio.sleep(delay)

    # Should not reach here
    return await _do_request()


async def rest_ensure_async(
    nb: object,
    path: str,
    *,
    lookup: dict[str, object],
    payload: dict[str, object],
) -> RestRecord:
    for candidate in _candidate_reuse_lookups(lookup, payload):
        existing = await rest_first_async(
            nb,
            path,
            query={**candidate, "limit": 2},
        )
        if existing:
            return existing
    try:
        return await rest_create_async(nb, path, payload)
    except ProxboxException as error:
        if _is_duplicate_error(error.detail):
            for candidate in _candidate_reuse_lookups(lookup, payload):
                retry = await rest_first_async(
                    nb,
                    path,
                    query={**candidate, "limit": 2},
                )
                if retry:
                    return retry
        raise


async def rest_reconcile_async(  # noqa: C901
    nb: object,
    path: str,
    *,
    lookup: dict[str, object],
    payload: dict[str, object],
    schema: type[BaseModel] | type[dict],
    current_normalizer,
    patchable_fields: set[str] | frozenset[str] | None = None,
) -> RestRecord:
    supports_model_validation = hasattr(schema, "model_validate") and hasattr(schema, "model_dump")

    if supports_model_validation:
        desired_model = schema.model_validate(payload)
        desired_payload = desired_model.model_dump(exclude_none=True, by_alias=True)
    else:
        desired_payload = {key: value for key, value in payload.items() if value is not None}

    async def _find_existing() -> RestRecord | None:
        for candidate in _candidate_reuse_lookups(lookup, desired_payload):
            existing_record = await rest_first_async(nb, path, query={**candidate, "limit": 2})
            if existing_record:
                return existing_record
        return None

    async def _scan_existing() -> RestRecord | None:
        """Walk paginated list results — demo NetBox can have >>200 rows so the first page may miss a match."""
        candidates = _candidate_reuse_lookups(lookup, desired_payload)
        page_size = 200
        max_offset = 10_000
        offset = 0
        while offset <= max_offset:
            records = await rest_list_async(
                nb,
                path,
                query={"limit": page_size, "offset": offset},
            )
            if not records:
                return None
            for record in records:
                try:
                    current_normalized = current_normalizer(record.serialize())
                    if supports_model_validation:
                        current_model = schema.model_validate(current_normalized)
                        current_payload = current_model.model_dump(exclude_none=True, by_alias=True)
                    else:
                        current_payload = {
                            key: value
                            for key, value in dict(current_normalized or {}).items()
                            if value is not None
                        }
                except Exception:
                    logger.debug(
                        "Skipping NetBox record during reconcile scan (validation failed)",
                        exc_info=True,
                    )
                    continue
                for candidate in candidates:
                    if all(current_payload.get(key) == value for key, value in candidate.items()):
                        return record
            if len(records) < page_size:
                return None
            offset += page_size
        return None

    async def _reconcile(existing_record: RestRecord) -> RestRecord:
        current_normalized = current_normalizer(existing_record.serialize())
        if supports_model_validation:
            current_model = schema.model_validate(current_normalized)
            current_payload = current_model.model_dump(exclude_none=True, by_alias=True)
        else:
            current_payload = {
                key: value
                for key, value in dict(current_normalized or {}).items()
                if value is not None
            }

        patch_payload = {
            key: value
            for key, value in desired_payload.items()
            if current_payload.get(key) != value
        }
        if patchable_fields is not None:
            allowed = {str(field) for field in patchable_fields}
            patch_payload = {key: value for key, value in patch_payload.items() if key in allowed}
        if patch_payload:
            for field, value in patch_payload.items():
                setattr(existing_record, field, value)
            await existing_record.save()
        return existing_record

    existing = await _find_existing()
    if existing is None:
        try:
            return await rest_create_async(nb, path, desired_payload)
        except ProxboxException:
            # Re-fetch and scan: list filters can miss rows (API quirks); duplicate errors
            # are not always phrased with "already exists" / "must be unique".
            existing = await _find_existing()
            if existing is None:
                existing = await _scan_existing()
            if existing is not None:
                return await _reconcile(existing)
            raise

    return await _reconcile(existing)


async def rest_patch_async(
    nb: object,
    path: str,
    record_id: int,
    payload: dict[str, object],
) -> dict[str, object]:
    """PATCH a single NetBox record by ID with the given fields."""
    api = _unwrap_api(nb)
    semaphore = _get_netbox_semaphore()

    normalized_path = _normalize_path(path)
    detail_path = _detail_path(normalized_path, record_id)

    async def _do_request() -> dict[str, object]:
        try:
            response = await api.client.request("PATCH", detail_path, payload=payload)
        except Exception as e:
            _handle_netbox_error(e, f"patch {path}")
            raise  # Early return via exception
        body = _extract_payload(response)
        if isinstance(body, dict):
            _invalidate_get_cache_for_record(api, normalized_path, body)
        else:
            _invalidate_get_cache_for_path(api, normalized_path)
            _invalidate_get_cache_for_path(api, detail_path)
        return body

    # Retry loop with semaphore and exponential backoff for transient errors
    max_retries = max(0, int(os.environ.get("PROXBOX_NETBOX_MAX_RETRIES", "5")))
    base_delay = float(os.environ.get("PROXBOX_NETBOX_RETRY_DELAY", "2.0"))

    for attempt in range(max_retries + 1):
        async with semaphore:
            try:
                return await _do_request()
            except Exception as e:
                if attempt == max_retries or not _is_transient_netbox_error(e):
                    raise
                delay = _compute_retry_delay(base_delay, attempt, e)
                pressure_note = " (NetBox overwhelmed)" if _is_netbox_overwhelmed_error(e) else ""
                logger.warning(
                    "NetBox patch failed%s (attempt %s/%s), retrying in %ss: %s",
                    pressure_note,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    str(e)[:200],
                )
        await asyncio.sleep(delay)

    # Should not reach here
    return await _do_request()


def nested_tag_payload(tag: object) -> list[dict[str, object]]:
    slug = getattr(tag, "slug", None)
    name = getattr(tag, "name", None)
    color = getattr(tag, "color", None)
    if isinstance(tag, dict):
        slug = slug or tag.get("slug")
        name = name or tag.get("name")
        color = color or tag.get("color")
    if not slug or not name:
        return []
    payload = {"name": name, "slug": slug}
    if color:
        payload["color"] = color
    return [payload]


async def ensure_tag_async(
    nb: object,
    *,
    name: str,
    slug: str,
    color: str,
    description: str,
) -> RestRecord:
    return await rest_reconcile_async(
        nb,
        "/api/extras/tags/",
        lookup={"slug": slug},
        payload={
            "name": name,
            "slug": slug,
            "color": color,
            "description": description,
        },
        schema=TagSchema,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "color": record.get("color"),
            "description": record.get("description"),
        },
        patchable_fields={"name", "color", "description"},
    )
