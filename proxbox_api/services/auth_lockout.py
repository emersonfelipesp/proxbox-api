"""Shared authentication lockout policy, identity, persistence, and metrics."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import ipaddress
import os
import secrets
import stat
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeAlias, cast

from sqlalchemy import case, delete, func, literal, or_, update
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api import database
from proxbox_api.database import AuthLockout, AuthLockoutMetric, AuthLockoutReservation
from proxbox_api.logger import logger

_DEFAULT_THRESHOLD = 5
_DEFAULT_WINDOW_SECONDS = 300
_DEFAULT_SOURCE_THRESHOLD = 50
_DEFAULT_MAX_BUCKETS = 10_000
_DEFAULT_MAX_IN_FLIGHT = 32
_DEFAULT_MAX_GLOBAL_IN_FLIGHT = 256
_DEFAULT_MAX_ACTIVE_KEYS = 32
_DEFAULT_VERIFICATION_MAX_SECONDS = 180.0
_MIN_THRESHOLD = 1
_MAX_THRESHOLD = 100
_MIN_WINDOW_SECONDS = 1
_MAX_WINDOW_SECONDS = 86_400
_MIN_SOURCE_THRESHOLD = 1
_MAX_SOURCE_THRESHOLD = 100_000
_MIN_MAX_BUCKETS = 2
_MAX_MAX_BUCKETS = 1_000_000
_MIN_MAX_IN_FLIGHT = 1
_MAX_MAX_IN_FLIGHT = 1_024
_MIN_MAX_GLOBAL_IN_FLIGHT = 1
_MAX_MAX_GLOBAL_IN_FLIGHT = 4_096
_MIN_MAX_ACTIVE_KEYS = 1
_MAX_MAX_ACTIVE_KEYS = 1_024
_MIN_VERIFICATION_MAX_SECONDS = 0.1
_MAX_VERIFICATION_MAX_SECONDS = 3_600.0
_SAFE_IDENTIFIER_LENGTH = 12
_MIN_CLEAR_PREFIX_LENGTH = 8
_CREDENTIAL_BUCKET = "credential"
_SOURCE_BUCKET = "source"
_IDENTITY_KEY_ENV = "PROXBOX_AUTH_LOCKOUT_HMAC_KEY"
_IDENTITY_KEY_FILE_ENV = "PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE"
_RESERVATION_LEASE_SECONDS = 60.0
_RESERVATION_FINALIZATION_GRACE_SECONDS = 3_600
_lockout_table = cast(Any, AuthLockout).__table__
_metrics_table = cast(Any, AuthLockoutMetric).__table__
_reservation_table = cast(Any, AuthLockoutReservation).__table__

IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork: TypeAlias = ipaddress.IPv4Network | ipaddress.IPv6Network


class LockoutConfigurationError(ValueError):
    """An authentication lockout environment value is invalid."""


class LockoutSelectionError(ValueError):
    """A local administrative clear selector is unsafe or ambiguous."""


class LockoutCapacityError(RuntimeError):
    """No inactive row can be evicted without weakening an active lockout."""


@dataclass(frozen=True, slots=True)
class AuthLockoutPolicy:
    """Validated failure threshold and fixed-window duration."""

    threshold: int = _DEFAULT_THRESHOLD
    window_seconds: int = _DEFAULT_WINDOW_SECONDS
    source_threshold: int = _DEFAULT_SOURCE_THRESHOLD
    max_buckets: int = _DEFAULT_MAX_BUCKETS
    max_in_flight: int = _DEFAULT_MAX_IN_FLIGHT
    max_global_in_flight: int = _DEFAULT_MAX_GLOBAL_IN_FLIGHT
    max_active_keys: int = _DEFAULT_MAX_ACTIVE_KEYS
    verification_max_seconds: float = _DEFAULT_VERIFICATION_MAX_SECONDS

    def __post_init__(self) -> None:
        if not _MIN_THRESHOLD <= self.threshold <= _MAX_THRESHOLD:
            raise LockoutConfigurationError(
                f"lockout threshold must be between {_MIN_THRESHOLD} and {_MAX_THRESHOLD}"
            )
        if not _MIN_WINDOW_SECONDS <= self.window_seconds <= _MAX_WINDOW_SECONDS:
            raise LockoutConfigurationError(
                "lockout window must be between "
                f"{_MIN_WINDOW_SECONDS} and {_MAX_WINDOW_SECONDS} seconds"
            )
        if not _MIN_SOURCE_THRESHOLD <= self.source_threshold <= _MAX_SOURCE_THRESHOLD:
            raise LockoutConfigurationError(
                "source lockout threshold must be between "
                f"{_MIN_SOURCE_THRESHOLD} and {_MAX_SOURCE_THRESHOLD}"
            )
        if not _MIN_MAX_BUCKETS <= self.max_buckets <= _MAX_MAX_BUCKETS:
            raise LockoutConfigurationError(
                f"lockout max buckets must be between {_MIN_MAX_BUCKETS} and {_MAX_MAX_BUCKETS}"
            )
        if not _MIN_MAX_IN_FLIGHT <= self.max_in_flight <= _MAX_MAX_IN_FLIGHT:
            raise LockoutConfigurationError(
                "verification concurrency must be between "
                f"{_MIN_MAX_IN_FLIGHT} and {_MAX_MAX_IN_FLIGHT}"
            )
        if not (
            _MIN_MAX_GLOBAL_IN_FLIGHT <= self.max_global_in_flight <= _MAX_MAX_GLOBAL_IN_FLIGHT
        ):
            raise LockoutConfigurationError(
                "global verification concurrency must be between "
                f"{_MIN_MAX_GLOBAL_IN_FLIGHT} and {_MAX_MAX_GLOBAL_IN_FLIGHT}"
            )
        if not _MIN_MAX_ACTIVE_KEYS <= self.max_active_keys <= _MAX_MAX_ACTIVE_KEYS:
            raise LockoutConfigurationError(
                "active API keys per verification must be between "
                f"{_MIN_MAX_ACTIVE_KEYS} and {_MAX_MAX_ACTIVE_KEYS}"
            )
        if not (
            _MIN_VERIFICATION_MAX_SECONDS
            <= self.verification_max_seconds
            <= _MAX_VERIFICATION_MAX_SECONDS
        ):
            raise LockoutConfigurationError(
                "verification maximum lifetime must be between "
                f"{_MIN_VERIFICATION_MAX_SECONDS:g} and "
                f"{_MAX_VERIFICATION_MAX_SECONDS:g} seconds"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AuthLockoutPolicy:
        """Load and validate process-level auth settings."""

        values = os.environ if environ is None else environ
        threshold = _parse_env_int(
            values,
            "PROXBOX_AUTH_LOCKOUT_THRESHOLD",
            _DEFAULT_THRESHOLD,
        )
        window_seconds = _parse_env_int(
            values,
            "PROXBOX_AUTH_LOCKOUT_WINDOW_SECONDS",
            _DEFAULT_WINDOW_SECONDS,
        )
        source_threshold = _parse_env_int(
            values,
            "PROXBOX_AUTH_LOCKOUT_SOURCE_THRESHOLD",
            _DEFAULT_SOURCE_THRESHOLD,
        )
        max_buckets = _parse_env_int(
            values,
            "PROXBOX_AUTH_LOCKOUT_MAX_BUCKETS",
            _DEFAULT_MAX_BUCKETS,
        )
        max_in_flight = _parse_env_int(
            values,
            "PROXBOX_AUTH_LOCKOUT_MAX_IN_FLIGHT",
            _DEFAULT_MAX_IN_FLIGHT,
        )
        max_global_in_flight = _parse_env_int(
            values,
            "PROXBOX_AUTH_LOCKOUT_MAX_GLOBAL_IN_FLIGHT",
            _DEFAULT_MAX_GLOBAL_IN_FLIGHT,
        )
        max_active_keys = _parse_env_int(
            values,
            "PROXBOX_AUTH_MAX_ACTIVE_KEYS",
            _DEFAULT_MAX_ACTIVE_KEYS,
        )
        verification_max_seconds = _parse_env_float(
            values,
            "PROXBOX_AUTH_LOCKOUT_VERIFICATION_MAX_SECONDS",
            _DEFAULT_VERIFICATION_MAX_SECONDS,
        )
        return cls(
            threshold=threshold,
            window_seconds=window_seconds,
            source_threshold=source_threshold,
            max_buckets=max_buckets,
            max_in_flight=max_in_flight,
            max_global_in_flight=max_global_in_flight,
            max_active_keys=max_active_keys,
            verification_max_seconds=verification_max_seconds,
        )


@dataclass(frozen=True, slots=True)
class AuthSourceContext:
    """Normalized network source plus the trust decision used to derive it."""

    source_ip: str
    trust_context: str = "direct"

    @property
    def canonical(self) -> str:
        """Return the stable, non-secret source component used for bucketing."""

        return f"{self.trust_context}|{self.abuse_source}"

    @property
    def abuse_source(self) -> str:
        """Aggregate IPv6 clients by /64 for abuse accounting and rate limiting."""

        try:
            address = ipaddress.ip_address(self.source_ip)
        except ValueError:
            return self.source_ip
        if isinstance(address, ipaddress.IPv6Address):
            network = ipaddress.ip_network(f"{address}/64", strict=False)
            return network.with_prefixlen
        return address.compressed


@dataclass(frozen=True, slots=True)
class LockoutIdentity:
    """Secret-free durable identity for one source and presented credential."""

    bucket_id: str
    source_bucket_id: str
    source_context: str
    credential_id: str

    @property
    def safe_id(self) -> str:
        """Return the documented short identifier suitable for logs and CLI output."""

        return self.bucket_id[:_SAFE_IDENTIFIER_LENGTH]


@dataclass(frozen=True, slots=True)
class LockoutState:
    """Pure state-machine representation of a durable lockout bucket."""

    attempts: int
    window_started_at: float
    locked_until: float | None
    updated_at: float

    def is_locked(self, now: float) -> bool:
        """Return whether this state is locked at ``now``; expiry is boundary-inclusive."""

        return self.locked_until is not None and now < self.locked_until


@dataclass(frozen=True, slots=True)
class FailureResult:
    """Credential and source-budget states produced by one failed request."""

    credential: LockoutState
    source: LockoutState
    reservation_token: str | None = field(default=None, repr=False)

    @property
    def attempts(self) -> int:
        """Retain the legacy credential-attempt accessor."""

        return self.credential.attempts

    def is_locked(self, now: float) -> bool:
        return self.credential.is_locked(now) or self.source.is_locked(now)

    def remaining_attempts(self, policy: AuthLockoutPolicy) -> int:
        """Return the tighter remaining credential or source failure budget."""

        return max(
            0,
            min(
                policy.threshold - self.credential.attempts,
                policy.source_threshold - self.source.attempts,
            ),
        )


def _parse_env_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise LockoutConfigurationError(f"{name} must be an integer") from exc


def _parse_env_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise LockoutConfigurationError(f"{name} must be a number") from exc


def parse_trusted_proxy_cidrs(raw: str) -> tuple[IPNetwork, ...]:
    """Parse explicit trusted proxy CIDRs, rejecting every malformed entry."""

    if not raw.strip():
        return ()
    networks: list[IPNetwork] = []
    for token in raw.split(","):
        candidate = token.strip()
        if not candidate:
            raise LockoutConfigurationError("PROXBOX_TRUSTED_PROXIES contains an empty entry")
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError as exc:
            raise LockoutConfigurationError(
                f"invalid PROXBOX_TRUSTED_PROXIES CIDR: {candidate}"
            ) from exc
    return tuple(networks)


def load_trusted_proxy_cidrs(environ: Mapping[str, str] | None = None) -> tuple[IPNetwork, ...]:
    """Load the explicit trusted proxy list; an empty list trusts no caller."""

    values = os.environ if environ is None else environ
    return parse_trusted_proxy_cidrs(values.get("PROXBOX_TRUSTED_PROXIES", ""))


def _fsync_directory(path: Path) -> None:
    """Persist a newly created key-file directory entry before startup succeeds."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_identity_key_runtime_lock = threading.RLock()
_runtime_identity_hmac_key: bytes | None = None


def _identity_key_path() -> Path:
    override = os.environ.get(_IDENTITY_KEY_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    database_path = database.sqlite_file_name
    if database_path is None:
        raise LockoutConfigurationError(
            "the database must be initialized before deriving the default "
            "PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE path"
        )
    return database_path.with_name(f"{database_path.name}.auth-lockout.key")


def _open_private_regular_file(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        os.close(descriptor)
        raise LockoutConfigurationError(
            f"{_IDENTITY_KEY_FILE_ENV} must be a private regular non-symlink file"
        )
    return descriptor


def _read_identity_key_file(*, allow_create: bool) -> str:  # noqa: C901
    key_path = _identity_key_path()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = key_path.with_name(f"{key_path.name}.lock")
    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    lock_descriptor: int | None = None
    try:
        lock_descriptor = os.open(lock_path, lock_flags, 0o600)
        lock_metadata = os.fstat(lock_descriptor)
        if not stat.S_ISREG(lock_metadata.st_mode):
            raise LockoutConfigurationError("identity-key lock must be a regular file")
        os.fchmod(lock_descriptor, 0o600)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        try:
            descriptor = _open_private_regular_file(key_path)
        except FileNotFoundError:
            if not allow_create:
                raise LockoutConfigurationError(
                    f"{_IDENTITY_KEY_FILE_ENV} is missing for an already-bound database: {key_path}"
                ) from None
            temporary_path = key_path.with_name(f".{key_path.name}.{secrets.token_hex(16)}.tmp")
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                create_flags |= os.O_NOFOLLOW
            created: int | None = os.open(temporary_path, create_flags, 0o600)
            try:
                try:
                    key_file = os.fdopen(created, "wb")
                except BaseException:
                    os.close(created)
                    raise
                created = None
                with key_file:
                    key_file.write(secrets.token_urlsafe(48).encode("utf-8"))
                    key_file.flush()
                    os.fsync(key_file.fileno())
                os.replace(temporary_path, key_path)
                _fsync_directory(key_path.parent)
            finally:
                if created is not None:
                    os.close(created)
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
            descriptor = _open_private_regular_file(key_path)
        try:
            material = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        if len(material) > 4096:
            raise LockoutConfigurationError(f"{_IDENTITY_KEY_FILE_ENV} must not exceed 4096 bytes")
        return material.decode("utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise LockoutConfigurationError(
            f"unable to read {_IDENTITY_KEY_FILE_ENV}: {key_path}"
        ) from exc
    finally:
        if lock_descriptor is not None:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)


def _load_identity_hmac_key(*, allow_create: bool) -> bytes:
    configured = os.environ.get(_IDENTITY_KEY_ENV, "").strip()
    if not configured:
        configured = _read_identity_key_file(allow_create=allow_create)
    if len(configured.encode("utf-8")) < 32:
        variable = (
            _IDENTITY_KEY_ENV
            if os.environ.get(_IDENTITY_KEY_ENV, "").strip()
            else _IDENTITY_KEY_FILE_ENV
        )
        raise LockoutConfigurationError(f"{variable} must contain at least 32 bytes")
    return hashlib.sha256(configured.encode("utf-8")).digest()


def _identity_key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(b"proxbox-auth-lockout-key-v1\0" + key).hexdigest()


def initialize_auth_lockout_identity_key(expected_fingerprint: str | None) -> str:
    """Load, verify, and pin exactly one key generation for this process."""

    global _runtime_identity_hmac_key

    with _identity_key_runtime_lock:
        _runtime_identity_hmac_key = None
        _identity_hmac_key.cache_clear()
        key = _load_identity_hmac_key(allow_create=expected_fingerprint is None)
        fingerprint = _identity_key_fingerprint(key)
        if expected_fingerprint is not None and not hmac.compare_digest(
            fingerprint,
            expected_fingerprint,
        ):
            raise LockoutConfigurationError(
                "the authentication lockout identity key does not match the database binding"
            )
        _runtime_identity_hmac_key = key
        return fingerprint


def clear_runtime_auth_lockout_identity_key() -> None:
    """Forget process-local key material after database shutdown or failed startup."""

    global _runtime_identity_hmac_key

    with _identity_key_runtime_lock:
        _runtime_identity_hmac_key = None
        _identity_hmac_key.cache_clear()


@lru_cache(maxsize=1)
def _identity_hmac_key() -> bytes:
    """Return only the key generation pinned by serialized database startup."""

    with _identity_key_runtime_lock:
        if _runtime_identity_hmac_key is None:
            raise LockoutConfigurationError(
                "authentication lockout identity key was not validated during startup"
            )
        return _runtime_identity_hmac_key


def auth_lockout_identity_key_fingerprint_from_material(material: str) -> str:
    """Derive the non-secret binding fingerprint for offline key recovery."""

    if len(material.encode("utf-8")) < 32:
        raise LockoutConfigurationError("identity-key material must contain at least 32 bytes")
    key = hashlib.sha256(material.encode("utf-8")).digest()
    return hashlib.sha256(b"proxbox-auth-lockout-key-v1\0" + key).hexdigest()


def validate_auth_lockout_identity_key() -> None:
    """Fail startup if lockout identities would differ across workers or restarts."""

    _identity_hmac_key()


def _normalize_ip(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        return None


def _is_trusted(ip: str, trusted_proxies: Sequence[IPNetwork]) -> bool:
    try:
        address: IPAddress = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(
        address.version == network.version and address in network for network in trusted_proxies
    )


def resolve_auth_source_context(
    peer_ip: str | None,
    forwarded_for: str | None,
    trusted_proxies: Sequence[IPNetwork],
) -> AuthSourceContext:
    """Resolve a normalized auth source without trusting headers from arbitrary callers."""

    normalized_peer = _normalize_ip(peer_ip) or "unknown"
    if not _is_trusted(normalized_peer, trusted_proxies):
        return AuthSourceContext(source_ip=normalized_peer, trust_context="direct")
    if not forwarded_for:
        return AuthSourceContext(source_ip=normalized_peer, trust_context="trusted-peer")

    raw_candidates = [token.strip() for token in forwarded_for.split(",")]
    normalized_candidates = [_normalize_ip(token) for token in raw_candidates]
    if not raw_candidates or any(candidate is None for candidate in normalized_candidates):
        return AuthSourceContext(
            source_ip=normalized_peer,
            trust_context="trusted-peer-invalid-forwarding",
        )

    candidates = [candidate for candidate in normalized_candidates if candidate is not None]
    for candidate in reversed(candidates):
        if not _is_trusted(candidate, trusted_proxies):
            return AuthSourceContext(source_ip=candidate, trust_context="trusted-forwarded")
    return AuthSourceContext(source_ip=candidates[0], trust_context="trusted-forwarded")


def build_lockout_identity(
    source: AuthSourceContext | str,
    api_key: str | None,
) -> LockoutIdentity:
    """Build a composite bucket without persisting the credential or its full fingerprint."""

    source_context = source.canonical if isinstance(source, AuthSourceContext) else source
    credential_material = api_key if api_key is not None else "<missing>"
    signing_key = _identity_hmac_key()
    credential_digest = hmac.new(
        signing_key,
        b"proxbox-auth-credential-v1\0" + credential_material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    source_bucket_id = hmac.new(
        signing_key,
        b"proxbox-auth-source-v1\0" + source_context.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    bucket_id = hmac.new(
        signing_key,
        b"proxbox-auth-bucket-v1\0"
        + source_context.encode("utf-8")
        + b"\0"
        + credential_digest.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return LockoutIdentity(
        bucket_id=bucket_id,
        source_bucket_id=source_bucket_id,
        source_context=source_context,
        credential_id=credential_digest[:_SAFE_IDENTIFIER_LENGTH],
    )


def transition_failed_attempt(
    state: LockoutState | None,
    policy: AuthLockoutPolicy,
    now: float,
) -> LockoutState:
    """Apply the canonical fixed-window failure transition without I/O."""

    if state is None or now >= state.window_started_at + policy.window_seconds:
        attempts = 1
        window_started_at = now
    else:
        attempts = state.attempts + 1
        window_started_at = state.window_started_at
    locked_until = (
        window_started_at + policy.window_seconds if attempts >= policy.threshold else None
    )
    return LockoutState(
        attempts=attempts,
        window_started_at=window_started_at,
        locked_until=locked_until,
        updated_at=now,
    )


def _record_failure_statement(
    *,
    bucket_id: str,
    bucket_type: str,
    source_context: str,
    credential_id: str,
    threshold: int,
    window_seconds: int,
    now: float,
):
    initial = LockoutState(
        attempts=1,
        window_started_at=now,
        locked_until=now + window_seconds if threshold == 1 else None,
        updated_at=now,
    )
    columns = _lockout_table.c
    expired = cast(Any, now >= columns.window_started_at + window_seconds)
    next_attempts = case((expired, 1), else_=columns.attempts + 1)
    next_window = case((expired, now), else_=columns.window_started_at)
    next_locked_until = case(
        (next_attempts >= threshold, next_window + window_seconds),
        else_=None,
    )
    return (
        sqlite_insert(_lockout_table)
        .values(
            bucket_id=bucket_id,
            bucket_type=bucket_type,
            source_context=source_context,
            credential_id=credential_id,
            attempts=initial.attempts,
            window_started_at=initial.window_started_at,
            locked_until=initial.locked_until,
            updated_at=initial.updated_at,
        )
        .on_conflict_do_update(
            index_elements=[columns.bucket_id],
            set_={
                "bucket_type": bucket_type,
                "source_context": source_context,
                "credential_id": credential_id,
                "attempts": next_attempts,
                "window_started_at": next_window,
                "locked_until": next_locked_until,
                "updated_at": now,
            },
        )
        .returning(
            columns.attempts,
            columns.window_started_at,
            columns.locked_until,
            columns.updated_at,
        )
    )


def _reserve_verification_statement(
    *,
    credential_bucket_id: str,
    source_bucket_id: str,
    max_in_flight: int,
    max_global_in_flight: int,
    lease_seconds: float,
    max_lifetime_seconds: float,
    reservation_token: str,
    now: float,
):
    """Atomically reserve one independently expiring verification token."""

    reservations = _reservation_table.c
    buckets = _lockout_table.c
    active_credential = (
        select(func.count())
        .select_from(_reservation_table)
        .where(
            reservations.credential_bucket_id == credential_bucket_id,
            reservations.expires_at > now,
            reservations.deadline_at > now,
        )
        .scalar_subquery()
    )
    active_source = (
        select(func.count())
        .select_from(_reservation_table)
        .where(
            reservations.source_bucket_id == source_bucket_id,
            reservations.expires_at > now,
            reservations.deadline_at > now,
        )
        .scalar_subquery()
    )
    active_global = (
        select(func.count())
        .select_from(_reservation_table)
        .where(
            reservations.expires_at > now,
            reservations.deadline_at > now,
        )
        .scalar_subquery()
    )
    active_locks = (
        select(func.count())
        .select_from(_lockout_table)
        .where(
            buckets.bucket_id.in_((credential_bucket_id, source_bucket_id)),
            buckets.locked_until > now,
        )
        .scalar_subquery()
    )
    admission_conditions = (
        active_credential < max_in_flight,
        active_source < max_in_flight,
        active_global < max_global_in_flight,
        active_locks == 0,
    )
    deadline_at = now + max_lifetime_seconds
    values = sa_select(
        literal(reservation_token),
        literal(credential_bucket_id),
        literal(source_bucket_id),
        literal(min(now + lease_seconds, deadline_at)),
        literal(deadline_at),
        literal(now),
    ).where(*admission_conditions)
    return (
        sqlite_insert(_reservation_table)
        .from_select(
            (
                reservations.token,
                reservations.credential_bucket_id,
                reservations.source_bucket_id,
                reservations.expires_at,
                reservations.deadline_at,
                reservations.created_at,
            ),
            values,
        )
        .on_conflict_do_nothing(index_elements=[reservations.token])
        .returning(reservations.token)
    )


def _renew_reservation_statement(
    *,
    credential_bucket_id: str,
    source_bucket_id: str,
    reservation_token: str,
    lease_seconds: float,
    now: float,
):
    """Extend one live owner token without reviving an already expired lease."""

    columns = _reservation_table.c
    renewed_expiry = case(
        (columns.deadline_at < now + lease_seconds, columns.deadline_at),
        else_=now + lease_seconds,
    )
    return (
        update(_reservation_table)
        .where(
            columns.token == reservation_token,
            columns.credential_bucket_id == credential_bucket_id,
            columns.source_bucket_id == source_bucket_id,
            columns.expires_at > now,
            columns.deadline_at > now,
        )
        .values(expires_at=renewed_expiry)
        .returning(columns.token)
    )


def reservation_lease_seconds() -> float:
    """Return the renewable owner-lease duration used by verification workers."""

    return _RESERVATION_LEASE_SECONDS


def default_verification_max_seconds() -> float:
    """Return the migration-safe default terminal lifetime for one verifier."""

    return _DEFAULT_VERIFICATION_MAX_SECONDS


def reservation_heartbeat_interval() -> float:
    """Renew often enough that normal scheduler jitter cannot expose an admission gap."""

    return max(0.01, reservation_lease_seconds() / 3)


def _consume_reservation_statement(
    *,
    credential_bucket_id: str,
    source_bucket_id: str,
    reservation_token: str,
):
    columns = _reservation_table.c
    return (
        delete(_reservation_table)
        .where(
            columns.token == reservation_token,
            columns.credential_bucket_id == credential_bucket_id,
            columns.source_bucket_id == source_bucket_id,
        )
        .returning(columns.token, columns.created_at, columns.deadline_at)
    )


def _compact_expired_reservations_statement(now: float):
    """Delete tokens beyond the supported post-expiry cleanup horizon."""

    columns = _reservation_table.c
    return (
        delete(_reservation_table)
        .where(
            or_(
                columns.expires_at <= now - _RESERVATION_FINALIZATION_GRACE_SECONDS,
                columns.deadline_at <= now - _RESERVATION_FINALIZATION_GRACE_SECONDS,
            )
        )
        .returning(columns.token)
    )


def _reservation_reference_exists(bucket_id: Any):
    reservations = _reservation_table.c
    return (
        sa_select(literal(1))
        .select_from(_reservation_table)
        .where(
            or_(
                reservations.credential_bucket_id == bucket_id,
                reservations.source_bucket_id == bucket_id,
            )
        )
        .exists()
    )


def _prune_expired_statement(policy: AuthLockoutPolicy, now: float):
    """Bound transient bucket growth using the configured inactivity window."""

    return delete(_lockout_table).where(
        _lockout_table.c.updated_at < now - policy.window_seconds,
        ~_reservation_reference_exists(_lockout_table.c.bucket_id),
    )


def _evict_stalest_expired_bucket_statement(
    bucket_type: str,
    policy: AuthLockoutPolicy,
    now: float,
):
    """Evict one expired unreserved row without weakening a live failure budget."""

    buckets = _lockout_table.c
    candidate = (
        select(buckets.bucket_id)
        .where(
            buckets.bucket_type == bucket_type,
            buckets.window_started_at + policy.window_seconds <= now,
            or_(buckets.locked_until.is_(None), buckets.locked_until <= now),
            ~_reservation_reference_exists(buckets.bucket_id),
        )
        .order_by(buckets.updated_at.asc(), buckets.bucket_id.asc())
        .limit(1)
        .scalar_subquery()
    )
    return delete(_lockout_table).where(buckets.bucket_id == candidate).returning(buckets.bucket_id)


def _record_metrics_statement(
    *,
    now: float,
    failure: int = 0,
    lockout: int = 0,
    source_lockout: int = 0,
    recovery: int = 0,
    capacity_rejection: int = 0,
    orphan_compaction: int = 0,
):
    columns = _metrics_table.c
    return (
        sqlite_insert(_metrics_table)
        .values(
            id=1,
            failures_total=failure,
            lockouts_total=lockout,
            source_lockouts_total=source_lockout,
            recoveries_total=recovery,
            capacity_rejections_total=capacity_rejection,
            orphan_compactions_total=orphan_compaction,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[columns.id],
            set_={
                "failures_total": columns.failures_total + failure,
                "lockouts_total": columns.lockouts_total + lockout,
                "source_lockouts_total": columns.source_lockouts_total + source_lockout,
                "recoveries_total": columns.recoveries_total + recovery,
                "capacity_rejections_total": (
                    columns.capacity_rejections_total + capacity_rejection
                ),
                "orphan_compactions_total": (columns.orphan_compactions_total + orphan_compaction),
                "updated_at": now,
            },
        )
    )


def _state_from_row(row: Sequence[Any]) -> LockoutState:
    attempts, window_started_at, locked_until, updated_at = row
    return LockoutState(
        attempts=int(attempts),
        window_started_at=float(window_started_at),
        locked_until=float(locked_until) if locked_until is not None else None,
        updated_at=float(updated_at),
    )


def _state_from_bucket(row: AuthLockout) -> LockoutState:
    return LockoutState(
        attempts=row.attempts,
        window_started_at=row.window_started_at,
        locked_until=row.locked_until,
        updated_at=row.updated_at,
    )


class AuthLockoutService:
    """The single sync/async persistence boundary for authentication lockouts."""

    @staticmethod
    def get(session: Session, identity: LockoutIdentity) -> AuthLockout | None:
        return session.get(AuthLockout, identity.bucket_id)

    @staticmethod
    def get_source(session: Session, identity: LockoutIdentity) -> AuthLockout | None:
        return session.get(AuthLockout, identity.source_bucket_id)

    @staticmethod
    async def get_async(
        session: AsyncSession,
        identity: LockoutIdentity,
    ) -> AuthLockout | None:
        return await session.get(AuthLockout, identity.bucket_id)

    @staticmethod
    async def get_source_async(
        session: AsyncSession,
        identity: LockoutIdentity,
    ) -> AuthLockout | None:
        return await session.get(AuthLockout, identity.source_bucket_id)

    @staticmethod
    def is_locked(session: Session, identity: LockoutIdentity, now: float | None = None) -> bool:
        rows = (
            AuthLockoutService.get(session, identity),
            AuthLockoutService.get_source(session, identity),
        )
        timestamp = time.time() if now is None else now
        return any(
            row is not None and row.locked_until is not None and timestamp < row.locked_until
            for row in rows
        )

    @staticmethod
    async def is_locked_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        now: float | None = None,
    ) -> bool:
        rows = (
            await AuthLockoutService.get_async(session, identity),
            await AuthLockoutService.get_source_async(session, identity),
        )
        timestamp = time.time() if now is None else now
        return any(
            row is not None and row.locked_until is not None and timestamp < row.locked_until
            for row in rows
        )

    @staticmethod
    def _compact_reservations_in_transaction(session: Session, now: float) -> int:
        compacted = session.exec(cast(Any, _compact_expired_reservations_statement(now))).all()
        if compacted:
            session.exec(
                cast(
                    Any,
                    _record_metrics_statement(
                        now=now,
                        orphan_compaction=len(compacted),
                    ),
                )
            )
        return len(compacted)

    @staticmethod
    async def _compact_reservations_in_transaction_async(
        session: AsyncSession,
        now: float,
    ) -> int:
        compacted = (
            await session.exec(cast(Any, _compact_expired_reservations_statement(now)))
        ).all()
        if compacted:
            await session.exec(
                cast(
                    Any,
                    _record_metrics_statement(
                        now=now,
                        orphan_compaction=len(compacted),
                    ),
                )
            )
        return len(compacted)

    @staticmethod
    def _coalesced_failure_result(
        session: Session,
        identity: LockoutIdentity,
        reservation_started_at: float,
    ) -> FailureResult | None:
        """Reuse a failure transition completed after this verification started."""

        credential = AuthLockoutService.get(session, identity)
        if credential is None or credential.updated_at <= reservation_started_at:
            return None
        source = AuthLockoutService.get_source(session, identity)
        source_state = (
            _state_from_bucket(source)
            if source is not None
            else LockoutState(0, reservation_started_at, None, reservation_started_at)
        )
        return FailureResult(
            credential=_state_from_bucket(credential),
            source=source_state,
        )

    @staticmethod
    async def _coalesced_failure_result_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        reservation_started_at: float,
    ) -> FailureResult | None:
        """Async counterpart to :meth:`_coalesced_failure_result`."""

        credential = await AuthLockoutService.get_async(session, identity)
        if credential is None or credential.updated_at <= reservation_started_at:
            return None
        source = await AuthLockoutService.get_source_async(session, identity)
        source_state = (
            _state_from_bucket(source)
            if source is not None
            else LockoutState(0, reservation_started_at, None, reservation_started_at)
        )
        return FailureResult(
            credential=_state_from_bucket(credential),
            source=source_state,
        )

    @staticmethod
    def reserve_verification(
        session: Session,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float | None = None,
    ) -> FailureResult | None:
        """Atomically admit one bcrypt verification with an independent token lease."""

        timestamp = time.time() if now is None else now
        reservation_token = secrets.token_hex(16)
        AuthLockoutService._compact_reservations_in_transaction(session, timestamp)
        admitted = session.exec(
            cast(
                Any,
                _reserve_verification_statement(
                    credential_bucket_id=identity.bucket_id,
                    source_bucket_id=identity.source_bucket_id,
                    max_in_flight=policy.max_in_flight,
                    max_global_in_flight=policy.max_global_in_flight,
                    lease_seconds=reservation_lease_seconds(),
                    max_lifetime_seconds=policy.verification_max_seconds,
                    reservation_token=reservation_token,
                    now=timestamp,
                ),
            )
        ).first()
        if admitted is None:
            if not AuthLockoutService.is_locked(session, identity, timestamp):
                session.exec(
                    cast(
                        Any,
                        _record_metrics_statement(
                            now=timestamp,
                            capacity_rejection=1,
                        ),
                    )
                )
            session.commit()
            return None
        credential_quota, source_quota = AuthLockoutService._partition_quotas(policy)
        AuthLockoutService._ensure_bucket_capacity_in_transaction(
            session,
            bucket_id=identity.bucket_id,
            bucket_type=_CREDENTIAL_BUCKET,
            policy=policy,
            quota=credential_quota,
            now=timestamp,
        )
        AuthLockoutService._ensure_bucket_capacity_in_transaction(
            session,
            bucket_id=identity.source_bucket_id,
            bucket_type=_SOURCE_BUCKET,
            policy=policy,
            quota=source_quota,
            now=timestamp,
        )
        session.commit()
        empty_state = LockoutState(0, timestamp, None, timestamp)
        return FailureResult(
            credential=empty_state,
            source=empty_state,
            reservation_token=reservation_token,
        )

    @staticmethod
    async def reserve_verification_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float | None = None,
    ) -> FailureResult | None:
        """Async counterpart to :meth:`reserve_verification`."""

        timestamp = time.time() if now is None else now
        reservation_token = secrets.token_hex(16)
        await AuthLockoutService._compact_reservations_in_transaction_async(session, timestamp)
        admitted = (
            await session.exec(
                cast(
                    Any,
                    _reserve_verification_statement(
                        credential_bucket_id=identity.bucket_id,
                        source_bucket_id=identity.source_bucket_id,
                        max_in_flight=policy.max_in_flight,
                        max_global_in_flight=policy.max_global_in_flight,
                        lease_seconds=reservation_lease_seconds(),
                        max_lifetime_seconds=policy.verification_max_seconds,
                        reservation_token=reservation_token,
                        now=timestamp,
                    ),
                )
            )
        ).first()
        if admitted is None:
            if not await AuthLockoutService.is_locked_async(session, identity, timestamp):
                await session.exec(
                    cast(
                        Any,
                        _record_metrics_statement(
                            now=timestamp,
                            capacity_rejection=1,
                        ),
                    )
                )
            await session.commit()
            return None
        credential_quota, source_quota = AuthLockoutService._partition_quotas(policy)
        await AuthLockoutService._ensure_bucket_capacity_in_transaction_async(
            session,
            bucket_id=identity.bucket_id,
            bucket_type=_CREDENTIAL_BUCKET,
            policy=policy,
            quota=credential_quota,
            now=timestamp,
        )
        await AuthLockoutService._ensure_bucket_capacity_in_transaction_async(
            session,
            bucket_id=identity.source_bucket_id,
            bucket_type=_SOURCE_BUCKET,
            policy=policy,
            quota=source_quota,
            now=timestamp,
        )
        await session.commit()
        empty_state = LockoutState(0, timestamp, None, timestamp)
        return FailureResult(
            credential=empty_state,
            source=empty_state,
            reservation_token=reservation_token,
        )

    @staticmethod
    def renew_verification(
        session: Session,
        identity: LockoutIdentity,
        reservation: FailureResult,
        now: float | None = None,
    ) -> bool:
        """Heartbeat one live reservation; an expired or foreign token stays expired."""

        reservation_token = reservation.reservation_token
        if reservation_token is None:
            raise ValueError("verification reservation has no token")
        timestamp = time.time() if now is None else now
        renewed = session.exec(
            cast(
                Any,
                _renew_reservation_statement(
                    credential_bucket_id=identity.bucket_id,
                    source_bucket_id=identity.source_bucket_id,
                    reservation_token=reservation_token,
                    lease_seconds=reservation_lease_seconds(),
                    now=timestamp,
                ),
            )
        ).first()
        session.commit()
        return renewed is not None

    @staticmethod
    async def renew_verification_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        reservation: FailureResult,
        now: float | None = None,
    ) -> bool:
        """Async counterpart to :meth:`renew_verification`."""

        reservation_token = reservation.reservation_token
        if reservation_token is None:
            raise ValueError("verification reservation has no token")
        timestamp = time.time() if now is None else now
        renewed = (
            await session.exec(
                cast(
                    Any,
                    _renew_reservation_statement(
                        credential_bucket_id=identity.bucket_id,
                        source_bucket_id=identity.source_bucket_id,
                        reservation_token=reservation_token,
                        lease_seconds=reservation_lease_seconds(),
                        now=timestamp,
                    ),
                )
            )
        ).first()
        await session.commit()
        return renewed is not None

    @staticmethod
    def cancel_verification(
        session: Session,
        identity: LockoutIdentity,
        reservation: FailureResult,
    ) -> bool:
        """Release one owner token without recording an authentication failure."""

        reservation_token = reservation.reservation_token
        if reservation_token is None:
            raise ValueError("verification reservation has no token")
        consumed = session.exec(
            cast(
                Any,
                _consume_reservation_statement(
                    credential_bucket_id=identity.bucket_id,
                    source_bucket_id=identity.source_bucket_id,
                    reservation_token=reservation_token,
                ),
            )
        ).first()
        session.commit()
        return consumed is not None

    @staticmethod
    async def cancel_verification_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        reservation: FailureResult,
    ) -> bool:
        """Async counterpart to :meth:`cancel_verification`."""

        reservation_token = reservation.reservation_token
        if reservation_token is None:
            raise ValueError("verification reservation has no token")
        consumed = (
            await session.exec(
                cast(
                    Any,
                    _consume_reservation_statement(
                        credential_bucket_id=identity.bucket_id,
                        source_bucket_id=identity.source_bucket_id,
                        reservation_token=reservation_token,
                    ),
                )
            )
        ).first()
        await session.commit()
        return consumed is not None

    @staticmethod
    def finalize_verification(
        session: Session,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        reservation: FailureResult,
        *,
        succeeded: bool,
        now: float | None = None,
    ) -> FailureResult | None:
        """Release an admission and atomically convert it to a durable failure."""

        timestamp = time.time() if now is None else now
        reservation_token = reservation.reservation_token
        if reservation_token is None:
            raise ValueError("verification reservation has no token")
        AuthLockoutService._compact_reservations_in_transaction(session, timestamp)
        consumed = session.exec(
            cast(
                Any,
                _consume_reservation_statement(
                    credential_bucket_id=identity.bucket_id,
                    source_bucket_id=identity.source_bucket_id,
                    reservation_token=reservation_token,
                ),
            )
        ).first()
        if consumed is None:
            session.commit()
            return None
        if timestamp >= float(consumed[2]):
            session.commit()
            return None
        if succeeded:
            session.commit()
            empty_state = LockoutState(0, timestamp, None, timestamp)
            return FailureResult(credential=empty_state, source=empty_state)
        reservation_started_at = float(consumed[1])
        coalesced = AuthLockoutService._coalesced_failure_result(
            session,
            identity,
            reservation_started_at,
        )
        if coalesced is not None:
            result, source_persisted = AuthLockoutService._record_source_failure_in_transaction(
                session,
                identity,
                policy,
                timestamp,
                credential_state=coalesced.credential,
            )
            session.commit()
            AuthLockoutService._log_new_lockouts(
                identity,
                policy,
                result,
                credential_persisted=False,
                source_persisted=source_persisted,
            )
            return result
        result, credential_persisted, source_persisted = (
            AuthLockoutService._record_failure_in_transaction(
                session,
                identity,
                policy,
                timestamp,
            )
        )
        session.commit()
        AuthLockoutService._log_new_lockouts(
            identity,
            policy,
            result,
            credential_persisted=credential_persisted,
            source_persisted=source_persisted,
        )
        return result

    @staticmethod
    async def finalize_verification_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        reservation: FailureResult,
        *,
        succeeded: bool,
        now: float | None = None,
    ) -> FailureResult | None:
        """Async counterpart to :meth:`finalize_verification`."""

        timestamp = time.time() if now is None else now
        reservation_token = reservation.reservation_token
        if reservation_token is None:
            raise ValueError("verification reservation has no token")
        await AuthLockoutService._compact_reservations_in_transaction_async(session, timestamp)
        consumed = (
            await session.exec(
                cast(
                    Any,
                    _consume_reservation_statement(
                        credential_bucket_id=identity.bucket_id,
                        source_bucket_id=identity.source_bucket_id,
                        reservation_token=reservation_token,
                    ),
                )
            )
        ).first()
        if consumed is None:
            await session.commit()
            return None
        if timestamp >= float(consumed[2]):
            await session.commit()
            return None
        if succeeded:
            await session.commit()
            empty_state = LockoutState(0, timestamp, None, timestamp)
            return FailureResult(credential=empty_state, source=empty_state)
        reservation_started_at = float(consumed[1])
        coalesced = await AuthLockoutService._coalesced_failure_result_async(
            session,
            identity,
            reservation_started_at,
        )
        if coalesced is not None:
            (
                result,
                source_persisted,
            ) = await AuthLockoutService._record_source_failure_in_transaction_async(
                session,
                identity,
                policy,
                timestamp,
                credential_state=coalesced.credential,
            )
            await session.commit()
            AuthLockoutService._log_new_lockouts(
                identity,
                policy,
                result,
                credential_persisted=False,
                source_persisted=source_persisted,
            )
            return result
        (
            result,
            credential_persisted,
            source_persisted,
        ) = await AuthLockoutService._record_failure_in_transaction_async(
            session,
            identity,
            policy,
            timestamp,
        )
        await session.commit()
        AuthLockoutService._log_new_lockouts(
            identity,
            policy,
            result,
            credential_persisted=credential_persisted,
            source_persisted=source_persisted,
        )
        return result

    @staticmethod
    def record_failure(
        session: Session,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float | None = None,
    ) -> FailureResult:
        timestamp = time.time() if now is None else now
        result, credential_persisted, source_persisted = (
            AuthLockoutService._record_failure_in_transaction(
                session,
                identity,
                policy,
                timestamp,
            )
        )
        session.commit()
        AuthLockoutService._log_new_lockouts(
            identity,
            policy,
            result,
            credential_persisted=credential_persisted,
            source_persisted=source_persisted,
        )
        return result

    @staticmethod
    def record_missing_credential_failure(
        session: Session,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float | None = None,
    ) -> FailureResult:
        """Charge a missing header only to its source-abuse budget."""

        timestamp = time.time() if now is None else now
        result, source_persisted = AuthLockoutService._record_source_failure_in_transaction(
            session,
            identity,
            policy,
            timestamp,
        )
        session.commit()
        AuthLockoutService._log_new_lockouts(
            identity,
            policy,
            result,
            credential_persisted=False,
            source_persisted=source_persisted,
        )
        return result

    @staticmethod
    async def record_failure_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float | None = None,
    ) -> FailureResult:
        timestamp = time.time() if now is None else now
        (
            result,
            credential_persisted,
            source_persisted,
        ) = await AuthLockoutService._record_failure_in_transaction_async(
            session,
            identity,
            policy,
            timestamp,
        )
        await session.commit()
        AuthLockoutService._log_new_lockouts(
            identity,
            policy,
            result,
            credential_persisted=credential_persisted,
            source_persisted=source_persisted,
        )
        return result

    @staticmethod
    async def record_missing_credential_failure_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float | None = None,
    ) -> FailureResult:
        """Async counterpart to :meth:`record_missing_credential_failure`."""

        timestamp = time.time() if now is None else now
        (
            result,
            source_persisted,
        ) = await AuthLockoutService._record_source_failure_in_transaction_async(
            session,
            identity,
            policy,
            timestamp,
        )
        await session.commit()
        AuthLockoutService._log_new_lockouts(
            identity,
            policy,
            result,
            credential_persisted=False,
            source_persisted=source_persisted,
        )
        return result

    @staticmethod
    def _partition_quotas(policy: AuthLockoutPolicy) -> tuple[int, int]:
        """Reserve bounded, independent row pools for credential and source state."""

        credential_quota = max(1, policy.max_buckets // 2)
        source_quota = policy.max_buckets - credential_quota
        return credential_quota, source_quota

    @staticmethod
    def _capacity_lockout_state(threshold: int, window_seconds: int, now: float) -> LockoutState:
        """Fail closed when no durable row can represent a new failure identity."""

        return LockoutState(
            attempts=threshold,
            window_started_at=now,
            locked_until=now + window_seconds,
            updated_at=now,
        )

    @staticmethod
    def _ensure_bucket_capacity_in_transaction(
        session: Session,
        *,
        bucket_id: str,
        bucket_type: str,
        policy: AuthLockoutPolicy,
        quota: int,
        now: float,
    ) -> bool:
        partition_rows = session.exec(
            select(func.count())
            .select_from(_lockout_table)
            .where(_lockout_table.c.bucket_type == bucket_type)
        ).one()
        if int(partition_rows) < quota:
            return True
        exists = session.exec(
            select(func.count())
            .select_from(_lockout_table)
            .where(_lockout_table.c.bucket_id == bucket_id)
        ).one()
        if exists:
            return True
        evicted = session.exec(
            cast(
                Any,
                _evict_stalest_expired_bucket_statement(bucket_type, policy, now),
            )
        ).first()
        return evicted is not None

    @staticmethod
    async def _ensure_bucket_capacity_in_transaction_async(
        session: AsyncSession,
        *,
        bucket_id: str,
        bucket_type: str,
        policy: AuthLockoutPolicy,
        quota: int,
        now: float,
    ) -> bool:
        partition_rows = (
            await session.exec(
                select(func.count())
                .select_from(_lockout_table)
                .where(_lockout_table.c.bucket_type == bucket_type)
            )
        ).one()
        if int(partition_rows) < quota:
            return True
        exists = (
            await session.exec(
                select(func.count())
                .select_from(_lockout_table)
                .where(_lockout_table.c.bucket_id == bucket_id)
            )
        ).one()
        if exists:
            return True
        evicted = (
            await session.exec(
                cast(
                    Any,
                    _evict_stalest_expired_bucket_statement(bucket_type, policy, now),
                )
            )
        ).first()
        return evicted is not None

    @staticmethod
    def _record_bucket_in_transaction(
        session: Session,
        *,
        bucket_id: str,
        bucket_type: str,
        source_context: str,
        credential_id: str,
        threshold: int,
        policy: AuthLockoutPolicy,
        quota: int,
        now: float,
    ) -> tuple[LockoutState, bool]:
        if not AuthLockoutService._ensure_bucket_capacity_in_transaction(
            session,
            bucket_id=bucket_id,
            bucket_type=bucket_type,
            policy=policy,
            quota=quota,
            now=now,
        ):
            return (
                AuthLockoutService._capacity_lockout_state(
                    threshold,
                    policy.window_seconds,
                    now,
                ),
                False,
            )
        row = session.exec(
            cast(
                Any,
                _record_failure_statement(
                    bucket_id=bucket_id,
                    bucket_type=bucket_type,
                    source_context=source_context,
                    credential_id=credential_id,
                    threshold=threshold,
                    window_seconds=policy.window_seconds,
                    now=now,
                ),
            )
        ).first()
        assert row is not None
        return _state_from_row(cast(Sequence[Any], row)), True

    @staticmethod
    async def _record_bucket_in_transaction_async(
        session: AsyncSession,
        *,
        bucket_id: str,
        bucket_type: str,
        source_context: str,
        credential_id: str,
        threshold: int,
        policy: AuthLockoutPolicy,
        quota: int,
        now: float,
    ) -> tuple[LockoutState, bool]:
        if not await AuthLockoutService._ensure_bucket_capacity_in_transaction_async(
            session,
            bucket_id=bucket_id,
            bucket_type=bucket_type,
            policy=policy,
            quota=quota,
            now=now,
        ):
            return (
                AuthLockoutService._capacity_lockout_state(
                    threshold,
                    policy.window_seconds,
                    now,
                ),
                False,
            )
        row = (
            await session.exec(
                cast(
                    Any,
                    _record_failure_statement(
                        bucket_id=bucket_id,
                        bucket_type=bucket_type,
                        source_context=source_context,
                        credential_id=credential_id,
                        threshold=threshold,
                        window_seconds=policy.window_seconds,
                        now=now,
                    ),
                )
            )
        ).first()
        assert row is not None
        return _state_from_row(cast(Sequence[Any], row)), True

    @staticmethod
    def _record_source_failure_in_transaction(
        session: Session,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float,
        *,
        credential_state: LockoutState | None = None,
    ) -> tuple[FailureResult, bool]:
        """Advance one consumed rejection without allocating a credential row."""

        session.exec(cast(Any, _prune_expired_statement(policy, now)))
        _, source_quota = AuthLockoutService._partition_quotas(policy)
        source_state, source_persisted = AuthLockoutService._record_bucket_in_transaction(
            session,
            bucket_id=identity.source_bucket_id,
            bucket_type=_SOURCE_BUCKET,
            source_context=identity.source_context,
            credential_id="",
            threshold=policy.source_threshold,
            policy=policy,
            quota=source_quota,
            now=now,
        )
        entered_source_lockout = source_state.attempts == policy.source_threshold
        session.exec(
            cast(
                Any,
                _record_metrics_statement(
                    now=now,
                    failure=1,
                    source_lockout=int(source_persisted and entered_source_lockout),
                    capacity_rejection=int(not source_persisted),
                ),
            )
        )
        resolved_credential_state = credential_state or LockoutState(0, now, None, now)
        return FailureResult(resolved_credential_state, source_state), source_persisted

    @staticmethod
    async def _record_source_failure_in_transaction_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float,
        *,
        credential_state: LockoutState | None = None,
    ) -> tuple[FailureResult, bool]:
        """Async counterpart to :meth:`_record_source_failure_in_transaction`."""

        await session.exec(cast(Any, _prune_expired_statement(policy, now)))
        _, source_quota = AuthLockoutService._partition_quotas(policy)
        (
            source_state,
            source_persisted,
        ) = await AuthLockoutService._record_bucket_in_transaction_async(
            session,
            bucket_id=identity.source_bucket_id,
            bucket_type=_SOURCE_BUCKET,
            source_context=identity.source_context,
            credential_id="",
            threshold=policy.source_threshold,
            policy=policy,
            quota=source_quota,
            now=now,
        )
        entered_source_lockout = source_state.attempts == policy.source_threshold
        await session.exec(
            cast(
                Any,
                _record_metrics_statement(
                    now=now,
                    failure=1,
                    source_lockout=int(source_persisted and entered_source_lockout),
                    capacity_rejection=int(not source_persisted),
                ),
            )
        )
        resolved_credential_state = credential_state or LockoutState(0, now, None, now)
        return FailureResult(resolved_credential_state, source_state), source_persisted

    @staticmethod
    def _record_failure_in_transaction(
        session: Session,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float,
    ) -> tuple[FailureResult, bool, bool]:
        session.exec(cast(Any, _prune_expired_statement(policy, now)))
        credential_quota, source_quota = AuthLockoutService._partition_quotas(policy)
        credential_state, credential_persisted = AuthLockoutService._record_bucket_in_transaction(
            session,
            bucket_id=identity.bucket_id,
            bucket_type=_CREDENTIAL_BUCKET,
            source_context=identity.source_context,
            credential_id=identity.credential_id,
            threshold=policy.threshold,
            policy=policy,
            quota=credential_quota,
            now=now,
        )
        source_state, source_persisted = AuthLockoutService._record_bucket_in_transaction(
            session,
            bucket_id=identity.source_bucket_id,
            bucket_type=_SOURCE_BUCKET,
            source_context=identity.source_context,
            credential_id="",
            threshold=policy.source_threshold,
            policy=policy,
            quota=source_quota,
            now=now,
        )
        entered_lockout = credential_state.attempts == policy.threshold
        entered_source_lockout = source_state.attempts == policy.source_threshold
        session.exec(
            cast(
                Any,
                _record_metrics_statement(
                    now=now,
                    failure=1,
                    lockout=int(credential_persisted and entered_lockout),
                    source_lockout=int(source_persisted and entered_source_lockout),
                    capacity_rejection=int(not credential_persisted or not source_persisted),
                ),
            )
        )
        return (
            FailureResult(credential=credential_state, source=source_state),
            credential_persisted,
            source_persisted,
        )

    @staticmethod
    async def _record_failure_in_transaction_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float,
    ) -> tuple[FailureResult, bool, bool]:
        await session.exec(cast(Any, _prune_expired_statement(policy, now)))
        credential_quota, source_quota = AuthLockoutService._partition_quotas(policy)
        (
            credential_state,
            credential_persisted,
        ) = await AuthLockoutService._record_bucket_in_transaction_async(
            session,
            bucket_id=identity.bucket_id,
            bucket_type=_CREDENTIAL_BUCKET,
            source_context=identity.source_context,
            credential_id=identity.credential_id,
            threshold=policy.threshold,
            policy=policy,
            quota=credential_quota,
            now=now,
        )
        (
            source_state,
            source_persisted,
        ) = await AuthLockoutService._record_bucket_in_transaction_async(
            session,
            bucket_id=identity.source_bucket_id,
            bucket_type=_SOURCE_BUCKET,
            source_context=identity.source_context,
            credential_id="",
            threshold=policy.source_threshold,
            policy=policy,
            quota=source_quota,
            now=now,
        )
        entered_lockout = credential_state.attempts == policy.threshold
        entered_source_lockout = source_state.attempts == policy.source_threshold
        await session.exec(
            cast(
                Any,
                _record_metrics_statement(
                    now=now,
                    failure=1,
                    lockout=int(credential_persisted and entered_lockout),
                    source_lockout=int(source_persisted and entered_source_lockout),
                    capacity_rejection=int(not credential_persisted or not source_persisted),
                ),
            )
        )
        return (
            FailureResult(credential=credential_state, source=source_state),
            credential_persisted,
            source_persisted,
        )

    @staticmethod
    def _log_new_lockouts(
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        result: FailureResult,
        *,
        credential_persisted: bool = True,
        source_persisted: bool = True,
    ) -> None:
        if credential_persisted and result.credential.attempts == policy.threshold:
            logger.warning(
                "Authentication credential bucket entered lockout",
                extra={
                    "auth_bucket_id": identity.safe_id,
                    "auth_source_context": identity.source_context,
                    "auth_credential_id": identity.credential_id,
                },
            )
        if source_persisted and result.source.attempts == policy.source_threshold:
            logger.warning(
                "Authentication source budget entered lockout",
                extra={
                    "auth_bucket_id": identity.source_bucket_id[:_SAFE_IDENTIFIER_LENGTH],
                    "auth_source_context": identity.source_context,
                },
            )

    @staticmethod
    def clear(session: Session, identity: LockoutIdentity) -> bool:
        row = AuthLockoutService.get(session, identity)
        if row is None:
            session.rollback()
            return False
        session.exec(
            cast(
                Any,
                delete(_reservation_table).where(
                    _reservation_table.c.credential_bucket_id == identity.bucket_id
                ),
            )
        )
        session.delete(row)
        session.exec(cast(Any, _record_metrics_statement(now=time.time(), recovery=1)))
        session.commit()
        return True

    @staticmethod
    async def clear_async(session: AsyncSession, identity: LockoutIdentity) -> bool:
        row = await AuthLockoutService.get_async(session, identity)
        if row is None:
            await session.rollback()
            return False
        await session.exec(
            cast(
                Any,
                delete(_reservation_table).where(
                    _reservation_table.c.credential_bucket_id == identity.bucket_id
                ),
            )
        )
        await session.delete(row)
        await session.exec(cast(Any, _record_metrics_statement(now=time.time(), recovery=1)))
        await session.commit()
        return True

    @staticmethod
    def list_rows(session: Session) -> list[AuthLockout]:
        statement = select(AuthLockout).order_by(_lockout_table.c.updated_at.desc())
        return list(session.exec(statement).all())

    @staticmethod
    def clear_by_safe_id(session: Session, safe_id: str) -> int:
        selector = safe_id.strip().lower()
        if len(selector) < _MIN_CLEAR_PREFIX_LENGTH or any(
            character not in "0123456789abcdef" for character in selector
        ):
            raise LockoutSelectionError(
                f"bucket ID must be at least {_MIN_CLEAR_PREFIX_LENGTH} hexadecimal characters"
            )
        matches = list(
            session.exec(
                select(AuthLockout).where(cast(Any, AuthLockout.bucket_id).startswith(selector))
            ).all()
        )
        if len(matches) > 1:
            raise LockoutSelectionError("bucket ID is ambiguous; provide more characters")
        if not matches:
            return 0
        bucket_id = matches[0].bucket_id
        session.exec(
            cast(
                Any,
                delete(_reservation_table).where(
                    or_(
                        _reservation_table.c.credential_bucket_id == bucket_id,
                        _reservation_table.c.source_bucket_id == bucket_id,
                    )
                ),
            )
        )
        session.delete(matches[0])
        session.exec(cast(Any, _record_metrics_statement(now=time.time(), recovery=1)))
        session.commit()
        return 1

    @staticmethod
    def clear_all(session: Session) -> int:
        session.exec(cast(Any, delete(_reservation_table)))
        result = session.exec(cast(Any, delete(_lockout_table)))
        cleared = int(cast(Any, result).rowcount or 0)
        if cleared:
            session.exec(cast(Any, _record_metrics_statement(now=time.time(), recovery=cleared)))
        session.commit()
        return cleared


def get_auth_lockout_metrics(session: Session, now: float | None = None) -> dict[str, int]:
    """Return aggregate lockout metrics without source or credential labels."""

    timestamp = time.time() if now is None else now
    active_credentials = session.exec(
        select(func.count())
        .select_from(_lockout_table)
        .where(
            _lockout_table.c.bucket_type == _CREDENTIAL_BUCKET,
            _lockout_table.c.locked_until > timestamp,
        )
    ).one()
    active_sources = session.exec(
        select(func.count())
        .select_from(_lockout_table)
        .where(
            _lockout_table.c.bucket_type == _SOURCE_BUCKET,
            _lockout_table.c.locked_until > timestamp,
        )
    ).one()
    row_count = session.exec(select(func.count()).select_from(_lockout_table)).one()
    active_verifications = session.exec(
        select(func.count())
        .select_from(_reservation_table)
        .where(
            _reservation_table.c.expires_at > timestamp,
            _reservation_table.c.deadline_at > timestamp,
        )
    ).one()
    expired_orphans = session.exec(
        select(func.count())
        .select_from(_reservation_table)
        .where(
            or_(
                _reservation_table.c.expires_at <= timestamp,
                _reservation_table.c.deadline_at <= timestamp,
            )
        )
    ).one()
    counters = session.get(AuthLockoutMetric, 1)
    return {
        "proxbox_auth_failures_total": counters.failures_total if counters else 0,
        "proxbox_auth_lockouts_total": counters.lockouts_total if counters else 0,
        "proxbox_auth_source_lockouts_total": counters.source_lockouts_total if counters else 0,
        "proxbox_auth_recoveries_total": counters.recoveries_total if counters else 0,
        "proxbox_auth_capacity_rejections_total": (
            counters.capacity_rejections_total if counters else 0
        ),
        "proxbox_auth_orphan_compactions_total": (
            counters.orphan_compactions_total if counters else 0
        ),
        "proxbox_auth_active_lockouts": int(active_credentials),
        "proxbox_auth_active_source_lockouts": int(active_sources),
        "proxbox_auth_bucket_rows": int(row_count),
        "proxbox_auth_verifications_in_flight": int(active_verifications),
        "proxbox_auth_expired_orphan_reservations": int(expired_orphans),
    }


def get_auth_lockout_prometheus_metrics(session: Session, now: float | None = None) -> str:
    """Return aggregate, label-free auth metrics in Prometheus exposition format."""

    metrics = get_auth_lockout_metrics(session, now)
    lines = [
        "# HELP proxbox_auth_failures_total Total rejected authentication attempts",
        "# TYPE proxbox_auth_failures_total counter",
        f"proxbox_auth_failures_total {metrics['proxbox_auth_failures_total']}",
        "# HELP proxbox_auth_lockouts_total Total credential buckets entering lockout",
        "# TYPE proxbox_auth_lockouts_total counter",
        f"proxbox_auth_lockouts_total {metrics['proxbox_auth_lockouts_total']}",
        "# HELP proxbox_auth_source_lockouts_total Total sources exhausting their failure budget",
        "# TYPE proxbox_auth_source_lockouts_total counter",
        f"proxbox_auth_source_lockouts_total {metrics['proxbox_auth_source_lockouts_total']}",
        "# HELP proxbox_auth_recoveries_total Total lockout buckets cleared",
        "# TYPE proxbox_auth_recoveries_total counter",
        f"proxbox_auth_recoveries_total {metrics['proxbox_auth_recoveries_total']}",
        "# HELP proxbox_auth_capacity_rejections_total Verification admissions or failure-row writes rejected by bounded capacity",
        "# TYPE proxbox_auth_capacity_rejections_total counter",
        f"proxbox_auth_capacity_rejections_total {metrics['proxbox_auth_capacity_rejections_total']}",
        "# HELP proxbox_auth_orphan_compactions_total Expired reservation tokens compacted after the supported cleanup horizon",
        "# TYPE proxbox_auth_orphan_compactions_total counter",
        f"proxbox_auth_orphan_compactions_total {metrics['proxbox_auth_orphan_compactions_total']}",
        "# HELP proxbox_auth_active_lockouts Current locked credential buckets",
        "# TYPE proxbox_auth_active_lockouts gauge",
        f"proxbox_auth_active_lockouts {metrics['proxbox_auth_active_lockouts']}",
        "# HELP proxbox_auth_active_source_lockouts Current locked source budgets",
        "# TYPE proxbox_auth_active_source_lockouts gauge",
        f"proxbox_auth_active_source_lockouts {metrics['proxbox_auth_active_source_lockouts']}",
        "# HELP proxbox_auth_bucket_rows Current durable lockout bucket rows",
        "# TYPE proxbox_auth_bucket_rows gauge",
        f"proxbox_auth_bucket_rows {metrics['proxbox_auth_bucket_rows']}",
        "# HELP proxbox_auth_verifications_in_flight Current unexpired bcrypt token leases",
        "# TYPE proxbox_auth_verifications_in_flight gauge",
        f"proxbox_auth_verifications_in_flight {metrics['proxbox_auth_verifications_in_flight']}",
        "# HELP proxbox_auth_expired_orphan_reservations Expired crash-recovery tokens retained within the cleanup horizon",
        "# TYPE proxbox_auth_expired_orphan_reservations gauge",
        f"proxbox_auth_expired_orphan_reservations {metrics['proxbox_auth_expired_orphan_reservations']}",
    ]
    return "\n".join(lines) + "\n"
