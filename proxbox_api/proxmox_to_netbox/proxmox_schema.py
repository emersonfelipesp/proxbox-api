"""Utilities to read generated Proxmox OpenAPI artifacts for mapping contracts."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from proxbox_api.logger import logger
from proxbox_api.proxmox_codegen.security import (
    MAX_AGGREGATE_DOCUMENT_BYTES,
    MAX_DOCUMENT_BYTES,
    MAX_ELIGIBLE_VERSIONS,
    OpenAPIDocumentValidation,
    SchemaLimitError,
    SchemaShapeError,
    SchemaValidationError,
    resolve_contained,
    validate_openapi_document_limits,
    validate_version_tag,
)
from proxbox_api.runtime_settings import runtime_codegen_enabled

DEFAULT_PROXMOX_OPENAPI_TAG = "latest"
RUNTIME_GENERATED_ROUTE_CACHE_FILENAME = "runtime_generated_routes_cache.json"
RUNTIME_GENERATED_ROUTE_CACHE_PROVENANCE_FILENAME = "runtime_generated_routes_cache.provenance.json"
PROVENANCE_FILENAME = "provenance.json"
_MAX_PROVENANCE_BYTES = 16 * 1024
_MAX_ROUTE_CACHE_BYTES = MAX_AGGREGATE_DOCUMENT_BYTES + MAX_DOCUMENT_BYTES
_QUARANTINE_LOCK_FILENAME = ".quarantine.lock"
_BUNDLED_OPENAPI_CACHE_MAX_SIZE = MAX_ELIGIBLE_VERSIONS
_BUNDLED_OPENAPI_CACHE_LOCK = threading.RLock()
_BUNDLED_OPENAPI_CACHE: OrderedDict[
    tuple[str, str], tuple[dict[str, object], OpenAPIDocumentValidation]
] = OrderedDict()


def get_user_generated_dir() -> Path:
    """Return the writable directory for runtime-generated Proxmox schemas.

    Priority: PROXBOX_GENERATED_DIR env var → XDG_DATA_HOME/proxbox/generated/proxmox
    → ~/.local/share/proxbox/generated/proxmox

    This path is used for schemas generated at runtime (e.g. via POST /proxmox/viewer/generate).
    Bundled schemas shipped with the package are under get_bundled_generated_dir().
    """
    env_dir = os.environ.get("PROXBOX_GENERATED_DIR")
    if env_dir:
        return Path(env_dir)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "proxbox" / "generated" / "proxmox"


def get_bundled_generated_dir() -> Path:
    """Return the read-only bundled schema directory shipped with the package."""
    return Path(__file__).resolve().parents[1] / "generated" / "proxmox"


def bundled_proxmox_openapi_path(version_tag: str) -> Path:
    """Return the bundled OpenAPI path for a validated version tag."""

    return resolve_contained(
        get_bundled_generated_dir(),
        validate_version_tag(version_tag),
        "openapi.json",
    )


def has_bundled_proxmox_schema(version_tag: str) -> bool:
    """Return whether an immutable bundled schema exists for a version tag."""

    return bundled_proxmox_openapi_path(version_tag).is_file()


def _read_regular_file(path: Path, *, maximum_bytes: int, limit_name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(f"Refusing non-regular file: {path}")
        if file_stat.st_size > maximum_bytes:
            raise SchemaLimitError(f"{limit_name} exceeds its {maximum_bytes}-byte limit.")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum_bytes:
            raise SchemaLimitError(f"{limit_name} exceeds its {maximum_bytes}-byte limit.")
        return data
    finally:
        os.close(descriptor)


def _parse_json_object(raw: bytes, *, description: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except RecursionError as error:
        raise SchemaLimitError(f"{description} nesting exceeds the supported limit.") from error
    except (UnicodeError, ValueError) as error:
        raise SchemaShapeError(f"{description} is not valid JSON.") from error
    if not isinstance(value, dict):
        raise SchemaShapeError(f"{description} must be a JSON object.")
    return value


def _cached_bundled_openapi(
    cache_key: tuple[str, str],
) -> tuple[dict[str, object], OpenAPIDocumentValidation] | None:
    with _BUNDLED_OPENAPI_CACHE_LOCK:
        cached = _BUNDLED_OPENAPI_CACHE.pop(cache_key, None)
        if cached is not None:
            _BUNDLED_OPENAPI_CACHE[cache_key] = cached
        return cached


def _cache_bundled_openapi(
    cache_key: tuple[str, str],
    document: dict[str, object],
    validation: OpenAPIDocumentValidation,
) -> None:
    with _BUNDLED_OPENAPI_CACHE_LOCK:
        stale_keys = [key for key in _BUNDLED_OPENAPI_CACHE if key[0] == cache_key[0]]
        for stale_key in stale_keys:
            _BUNDLED_OPENAPI_CACHE.pop(stale_key)
        _BUNDLED_OPENAPI_CACHE[cache_key] = (document, validation)
        while len(_BUNDLED_OPENAPI_CACHE) > _BUNDLED_OPENAPI_CACHE_MAX_SIZE:
            _BUNDLED_OPENAPI_CACHE.popitem(last=False)


def _load_bundled_openapi(path: Path, raw: bytes) -> dict[str, object]:
    cache_key = (str(path), sha256(raw).hexdigest())
    with _BUNDLED_OPENAPI_CACHE_LOCK:
        cached = _cached_bundled_openapi(cache_key)
        if cached is not None:
            return cached[0]
        document = _parse_json_object(raw, description="OpenAPI document")
        validation = validate_openapi_document_limits(document)
        _cache_bundled_openapi(cache_key, document, validation)
        return document


def bundled_openapi_document_validation(
    document: dict[str, object],
) -> OpenAPIDocumentValidation | None:
    """Return the cached validation bound to an immutable bundled document."""

    with _BUNDLED_OPENAPI_CACHE_LOCK:
        for _, (cached_document, validation) in reversed(_BUNDLED_OPENAPI_CACHE.items()):
            if cached_document is document:
                return validation
    return None


def _read_provenance(path: Path) -> dict[str, object] | None:
    try:
        raw = _read_regular_file(
            path,
            maximum_bytes=_MAX_PROVENANCE_BYTES,
            limit_name="Provenance sidecar",
        )
        return _parse_json_object(raw, description="Provenance sidecar")
    except (OSError, SchemaValidationError):
        return None


def provenance_verified_artifact_bytes(
    artifact_path: Path,
    provenance_path: Path,
    *,
    maximum_bytes: int = _MAX_ROUTE_CACHE_BYTES,
) -> bytes | None:
    """Return bounded regular-file bytes when the provenance digest matches."""

    try:
        artifact = _read_regular_file(
            artifact_path,
            maximum_bytes=maximum_bytes,
            limit_name="Generated artifact",
        )
        provenance = _read_provenance(provenance_path)
        if provenance is None:
            return None
        expected_digest = provenance.get("sha256")
        source_url = provenance.get("source_url")
        generated_at = provenance.get("generated_at")
        matches = (
            isinstance(source_url, str)
            and bool(source_url)
            and isinstance(generated_at, str)
            and bool(generated_at)
            and isinstance(expected_digest, str)
            and len(expected_digest) == 64
            and expected_digest == sha256(artifact).hexdigest()
        )
        return artifact if matches else None
    except (OSError, SchemaValidationError):
        return None


def provenance_matches_artifact(artifact_path: Path, provenance_path: Path) -> bool:
    """Validate a generated artifact against its bounded JSON provenance sidecar."""

    return provenance_verified_artifact_bytes(artifact_path, provenance_path) is not None


def _user_openapi_artifact(version_tag: str) -> tuple[Path, bytes] | None:
    root = get_user_generated_dir()
    openapi_path = resolve_contained(root, version_tag, "openapi.json")
    provenance_path = resolve_contained(root, version_tag, PROVENANCE_FILENAME)
    try:
        artifact = _read_regular_file(
            openapi_path,
            maximum_bytes=MAX_DOCUMENT_BYTES,
            limit_name="OpenAPI document",
        )
    except (OSError, SchemaValidationError):
        artifact = None
    provenance = _read_provenance(provenance_path)
    expected_digest = provenance.get("sha256") if provenance else None
    source_url = provenance.get("source_url") if provenance else None
    generated_at = provenance.get("generated_at") if provenance else None
    if (
        artifact is not None
        and isinstance(expected_digest, str)
        and len(expected_digest) == 64
        and expected_digest == sha256(artifact).hexdigest()
        and isinstance(source_url, str)
        and bool(source_url)
        and isinstance(generated_at, str)
        and bool(generated_at)
    ):
        return openapi_path, artifact
    if _lexists(openapi_path) or _lexists(provenance_path):
        logger.warning(
            "Ignoring user-generated Proxmox OpenAPI artifact with missing or invalid provenance: %s",
            openapi_path,
        )
    return None


def _user_openapi_path(version_tag: str) -> Path | None:
    artifact = _user_openapi_artifact(version_tag)
    return artifact[0] if artifact is not None else None


def proxmox_generated_openapi_path(
    version_tag: str = DEFAULT_PROXMOX_OPENAPI_TAG,
    *,
    allow_user: bool | None = None,
) -> Path:
    """Return the immutable bundled or provenance-verified user OpenAPI path."""

    version_tag = validate_version_tag(version_tag)
    bundled_path = bundled_proxmox_openapi_path(version_tag)
    if bundled_path.is_file():
        return bundled_path
    if allow_user is None:
        allow_user = runtime_codegen_enabled()
    if not allow_user:
        return bundled_path
    try:
        user_path = _user_openapi_path(version_tag)
    except ValueError as error:
        logger.warning(
            "Ignoring uncontained user-generated Proxmox OpenAPI artifact for %s: %s",
            version_tag,
            error,
        )
        user_path = None
    return user_path if user_path is not None else bundled_path


def proxmox_generated_openapi_root() -> Path:
    """Return the bundled directory containing pre-shipped Proxmox OpenAPI artifacts."""

    return get_bundled_generated_dir()


def proxmox_generated_route_cache_path() -> Path:
    """Return the cache manifest path for runtime-generated Proxmox routes.

    Written to the user-writable location so it works in read-only packaged installs.
    """
    return resolve_contained(get_user_generated_dir(), RUNTIME_GENERATED_ROUTE_CACHE_FILENAME)


def proxmox_generated_route_cache_provenance_path() -> Path:
    """Return the provenance sidecar path for the runtime route cache."""

    return resolve_contained(
        get_user_generated_dir(),
        RUNTIME_GENERATED_ROUTE_CACHE_PROVENANCE_FILENAME,
    )


def _bundled_versions() -> set[str]:
    versions: set[str] = set()
    bundled_root = get_bundled_generated_dir()
    if bundled_root.exists():
        for child in bundled_root.iterdir():
            try:
                version_tag = validate_version_tag(child.name)
                if child.is_dir() and bundled_proxmox_openapi_path(version_tag).is_file():
                    versions.add(version_tag)
            except ValueError:
                continue
    return versions


def _add_user_versions(versions: set[str]) -> None:
    user_root = get_user_generated_dir()
    if not user_root.exists():
        return
    for child in user_root.iterdir():
        try:
            version_tag = validate_version_tag(child.name)
            if version_tag in versions:
                continue
            if child.is_dir() and load_proxmox_generated_openapi(version_tag, allow_user=True):
                versions.add(version_tag)
        except ValueError:
            continue


def available_proxmox_sdk_versions(*, include_user: bool | None = None) -> list[str]:
    """List generated Proxmox version tags that have an available OpenAPI artifact.

    Bundled tags win. User tags require a matching provenance sidecar and digest.
    """

    versions = _bundled_versions()

    if include_user is None:
        include_user = runtime_codegen_enabled()
    if not include_user:
        return sorted(versions)
    _add_user_versions(versions)
    return sorted(versions)


def load_proxmox_generated_openapi(
    version_tag: str = DEFAULT_PROXMOX_OPENAPI_TAG,
    *,
    allow_user: bool | None = None,
) -> dict[str, object]:
    """Load generated Proxmox OpenAPI document for version tag if available."""

    version_tag = validate_version_tag(version_tag)
    path = bundled_proxmox_openapi_path(version_tag)
    raw: bytes | None = None
    bundled = False
    try:
        if path.is_file():
            bundled = True
            raw = _read_regular_file(
                path,
                maximum_bytes=MAX_DOCUMENT_BYTES,
                limit_name="OpenAPI document",
            )
        else:
            if allow_user is None:
                allow_user = runtime_codegen_enabled()
            artifact = _user_openapi_artifact(version_tag) if allow_user else None
            if artifact is not None:
                path, raw = artifact
    except (OSError, ValueError) as error:
        logger.warning("Failed to read generated Proxmox OpenAPI from %s: %s", path, error)
        return {}
    if raw is None:
        logger.warning("Generated Proxmox OpenAPI artifact not found at %s", path)
        return {}
    try:
        if bundled:
            return _load_bundled_openapi(path, raw)
        document = _parse_json_object(raw, description="OpenAPI document")
        validate_openapi_document_limits(document)
        return document
    except SchemaValidationError as error:
        logger.warning("Failed to load generated Proxmox OpenAPI from %s: %s", path, error)
        return {}


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _quarantined_path(path: Path, timestamp: str) -> Path:
    candidate = path.with_name(f"{path.name}.quarantined-{timestamp}")
    suffix = 1
    while _lexists(candidate):
        candidate = path.with_name(f"{path.name}.quarantined-{timestamp}-{suffix}")
        suffix += 1
    return candidate


def _quarantine_file(path: Path, timestamp: str) -> Path:
    while True:
        destination = _quarantined_path(path, timestamp)
        try:
            os.link(path, destination, follow_symlinks=False)
        except FileExistsError:
            continue
        path.unlink()
        logger.warning("Quarantined legacy Proxmox codegen artifact: %s", destination)
        return destination


@contextmanager
def _quarantine_lock(root: Path) -> Iterator[None]:
    lock_path = root.resolve() / _QUARANTINE_LOCK_FILENAME
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _quarantine_if_present(path: Path, timestamp: str, quarantined: list[Path]) -> None:
    try:
        quarantined.append(_quarantine_file(path, timestamp))
    except FileNotFoundError:
        return


def quarantine_legacy_codegen_artifacts() -> list[Path]:
    """Quarantine persisted Python models and unprovenanced runtime caches."""

    root = get_user_generated_dir()
    if not root.exists():
        return []
    quarantined: list[Path] = []
    with _quarantine_lock(root):
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        resolved_root = root.resolve()
        for path in sorted(root.rglob("pydantic_models.py")):
            if path.parent.resolve().is_relative_to(resolved_root):
                _quarantine_if_present(path, timestamp, quarantined)

        cache_path = root / RUNTIME_GENERATED_ROUTE_CACHE_FILENAME
        cache_provenance_path = root / RUNTIME_GENERATED_ROUTE_CACHE_PROVENANCE_FILENAME
        cache_present = _lexists(cache_path)
        provenance_present = _lexists(cache_provenance_path)
        cache_is_invalid = cache_present and not provenance_matches_artifact(
            cache_path, cache_provenance_path
        )
        if cache_is_invalid:
            _quarantine_if_present(cache_path, timestamp, quarantined)
            _quarantine_if_present(cache_provenance_path, timestamp, quarantined)
        elif provenance_present and not cache_present:
            _quarantine_if_present(cache_provenance_path, timestamp, quarantined)
    return quarantined


def best_matching_version(release_tag: str) -> str | None:
    """Find the best available bundled schema version for a given release tag.

    Checks exact match first, then falls back to highest same-major version,
    then to "latest". Returns None only when no schemas are available at all.
    """
    available = available_proxmox_sdk_versions()
    if not available:
        return None

    # Exact match
    if release_tag in available:
        return release_tag

    # Highest version sharing the same major number
    major = release_tag.split(".")[0] if "." in release_tag else release_tag
    candidates = sorted(
        (v for v in available if v != "latest" and v.split(".")[0] == major),
        reverse=True,
    )
    if candidates:
        return candidates[0]

    # Fall back to "latest"
    if "latest" in available:
        return "latest"

    return available[0]


def proxmox_operation_schema(
    path: str,
    method: str,
    version_tag: str = DEFAULT_PROXMOX_OPENAPI_TAG,
    openapi: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """Get operation schema from generated Proxmox OpenAPI by path and method."""

    document = openapi or load_proxmox_generated_openapi(version_tag=version_tag)
    paths = document.get("paths", {}) if isinstance(document, dict) else {}
    item = paths.get(path)
    if not isinstance(item, dict):
        return None
    operation = item.get(method.lower())
    if not isinstance(operation, dict):
        return None
    return operation
