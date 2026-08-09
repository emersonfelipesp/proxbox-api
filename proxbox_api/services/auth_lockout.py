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

from sqlalchemy import case, delete, func, literal, or_
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
_SAFE_IDENTIFIER_LENGTH = 12
_MIN_CLEAR_PREFIX_LENGTH = 8
_CREDENTIAL_BUCKET = "credential"
_SOURCE_BUCKET = "source"
_IDENTITY_KEY_ENV = "PROXBOX_AUTH_LOCKOUT_HMAC_KEY"
_IDENTITY_KEY_FILE_ENV = "PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE"
_MIN_RESERVATION_LEASE_SECONDS = 60
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
        return cls(
            threshold=threshold,
            window_seconds=window_seconds,
            source_threshold=source_threshold,
            max_buckets=max_buckets,
            max_in_flight=max_in_flight,
            max_global_in_flight=max_global_in_flight,
        )


@dataclass(frozen=True, slots=True)
class AuthSourceContext:
    """Normalized network source plus the trust decision used to derive it."""

    source_ip: str
    trust_context: str = "direct"

    @property
    def canonical(self) -> str:
        """Return the stable, non-secret source component used for bucketing."""

        return f"{self.trust_context}|{self.source_ip}"


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
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                create_flags |= os.O_NOFOLLOW
            created = os.open(key_path, create_flags, 0o600)
            try:
                os.write(created, secrets.token_urlsafe(48).encode("utf-8"))
                os.fsync(created)
            finally:
                os.close(created)
            _fsync_directory(key_path.parent)
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
    lease_seconds: int,
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
        )
        .scalar_subquery()
    )
    active_source = (
        select(func.count())
        .select_from(_reservation_table)
        .where(
            reservations.source_bucket_id == source_bucket_id,
            reservations.expires_at > now,
        )
        .scalar_subquery()
    )
    active_global = (
        select(func.count())
        .select_from(_reservation_table)
        .where(reservations.expires_at > now)
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
    values = sa_select(
        literal(reservation_token),
        literal(credential_bucket_id),
        literal(source_bucket_id),
        literal(now + lease_seconds),
        literal(now),
    ).where(
        active_credential < max_in_flight,
        active_source < max_in_flight,
        active_global < max_global_in_flight,
        active_locks == 0,
    )
    return (
        sqlite_insert(_reservation_table)
        .from_select(
            (
                reservations.token,
                reservations.credential_bucket_id,
                reservations.source_bucket_id,
                reservations.expires_at,
                reservations.created_at,
            ),
            values,
        )
        .on_conflict_do_nothing(index_elements=[reservations.token])
        .returning(reservations.token)
    )


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
        .returning(columns.token)
    )


def _compact_expired_reservations_statement(now: float):
    """Delete tokens beyond the supported late-finalization horizon."""

    columns = _reservation_table.c
    return (
        delete(_reservation_table)
        .where(columns.expires_at <= now - _RESERVATION_FINALIZATION_GRACE_SECONDS)
        .returning(columns.token)
    )


def _prune_expired_statement(policy: AuthLockoutPolicy, now: float):
    """Bound transient bucket growth using the configured inactivity window."""

    return delete(_lockout_table).where(
        _lockout_table.c.updated_at < now - policy.window_seconds,
    )


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
    def reserve_verification(
        session: Session,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float | None = None,
    ) -> FailureResult | None:
        """Atomically admit one bcrypt verification with an independent token lease."""

        timestamp = time.time() if now is None else now
        reservation_token = secrets.token_hex(16)
        lease_seconds = max(policy.window_seconds, _MIN_RESERVATION_LEASE_SECONDS)
        compacted = session.exec(
            cast(Any, _compact_expired_reservations_statement(timestamp))
        ).all()
        if compacted:
            session.exec(
                cast(
                    Any,
                    _record_metrics_statement(
                        now=timestamp,
                        orphan_compaction=len(compacted),
                    ),
                )
            )
        admitted = session.exec(
            cast(
                Any,
                _reserve_verification_statement(
                    credential_bucket_id=identity.bucket_id,
                    source_bucket_id=identity.source_bucket_id,
                    max_in_flight=policy.max_in_flight,
                    max_global_in_flight=policy.max_global_in_flight,
                    lease_seconds=lease_seconds,
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
        lease_seconds = max(policy.window_seconds, _MIN_RESERVATION_LEASE_SECONDS)
        compacted = (
            await session.exec(cast(Any, _compact_expired_reservations_statement(timestamp)))
        ).all()
        if compacted:
            await session.exec(
                cast(
                    Any,
                    _record_metrics_statement(
                        now=timestamp,
                        orphan_compaction=len(compacted),
                    ),
                )
            )
        admitted = (
            await session.exec(
                cast(
                    Any,
                    _reserve_verification_statement(
                        credential_bucket_id=identity.bucket_id,
                        source_bucket_id=identity.source_bucket_id,
                        max_in_flight=policy.max_in_flight,
                        max_global_in_flight=policy.max_global_in_flight,
                        lease_seconds=lease_seconds,
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
        await session.commit()
        empty_state = LockoutState(0, timestamp, None, timestamp)
        return FailureResult(
            credential=empty_state,
            source=empty_state,
            reservation_token=reservation_token,
        )

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
            session.rollback()
            return None
        if succeeded:
            session.commit()
            empty_state = LockoutState(0, timestamp, None, timestamp)
            return FailureResult(credential=empty_state, source=empty_state)
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
            await session.rollback()
            return None
        if succeeded:
            await session.commit()
            empty_state = LockoutState(0, timestamp, None, timestamp)
            return FailureResult(credential=empty_state, source=empty_state)
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
    def _partition_quotas(policy: AuthLockoutPolicy) -> tuple[int, int]:
        """Reserve bounded, independent row pools for credential and source state."""

        credential_quota = max(1, policy.max_buckets // 2)
        source_quota = policy.max_buckets - credential_quota
        return credential_quota, source_quota

    @staticmethod
    def _transient_failure_state(threshold: int, window_seconds: int, now: float) -> LockoutState:
        """Represent a rejected attempt when its partition is saturated."""

        return LockoutState(
            attempts=1,
            window_started_at=now,
            locked_until=now + window_seconds if threshold == 1 else None,
            updated_at=now,
        )

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
        exists = session.exec(
            select(func.count())
            .select_from(_lockout_table)
            .where(_lockout_table.c.bucket_id == bucket_id)
        ).one()
        if not exists:
            partition_rows = session.exec(
                select(func.count())
                .select_from(_lockout_table)
                .where(_lockout_table.c.bucket_type == bucket_type)
            ).one()
            if int(partition_rows) >= quota:
                return (
                    AuthLockoutService._transient_failure_state(
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
        exists = (
            await session.exec(
                select(func.count())
                .select_from(_lockout_table)
                .where(_lockout_table.c.bucket_id == bucket_id)
            )
        ).one()
        if not exists:
            partition_rows = (
                await session.exec(
                    select(func.count())
                    .select_from(_lockout_table)
                    .where(_lockout_table.c.bucket_type == bucket_type)
                )
            ).one()
            if int(partition_rows) >= quota:
                return (
                    AuthLockoutService._transient_failure_state(
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
        .where(_reservation_table.c.expires_at > timestamp)
    ).one()
    expired_orphans = session.exec(
        select(func.count())
        .select_from(_reservation_table)
        .where(_reservation_table.c.expires_at <= timestamp)
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
        "# HELP proxbox_auth_capacity_rejections_total Verification admissions rejected or failed identities dropped by bounded capacity",
        "# TYPE proxbox_auth_capacity_rejections_total counter",
        f"proxbox_auth_capacity_rejections_total {metrics['proxbox_auth_capacity_rejections_total']}",
        "# HELP proxbox_auth_orphan_compactions_total Expired reservation tokens compacted after the supported finalization horizon",
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
        "# HELP proxbox_auth_expired_orphan_reservations Expired crash-recovery tokens retained within the late-finalization horizon",
        "# TYPE proxbox_auth_expired_orphan_reservations gauge",
        f"proxbox_auth_expired_orphan_reservations {metrics['proxbox_auth_expired_orphan_reservations']}",
    ]
    return "\n".join(lines) + "\n"
