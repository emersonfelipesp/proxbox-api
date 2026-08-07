"""Shared authentication lockout policy, identity, persistence, and metrics."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeAlias, cast

from sqlalchemy import case, delete, func, or_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api import database
from proxbox_api.database import AuthLockout, AuthLockoutMetric
from proxbox_api.logger import logger

_DEFAULT_THRESHOLD = 5
_DEFAULT_WINDOW_SECONDS = 300
_DEFAULT_SOURCE_THRESHOLD = 50
_DEFAULT_MAX_BUCKETS = 10_000
_MIN_THRESHOLD = 1
_MAX_THRESHOLD = 100
_MIN_WINDOW_SECONDS = 1
_MAX_WINDOW_SECONDS = 86_400
_MIN_SOURCE_THRESHOLD = 1
_MAX_SOURCE_THRESHOLD = 100_000
_MIN_MAX_BUCKETS = 2
_MAX_MAX_BUCKETS = 1_000_000
_SAFE_IDENTIFIER_LENGTH = 12
_MIN_CLEAR_PREFIX_LENGTH = 8
_CREDENTIAL_BUCKET = "credential"
_SOURCE_BUCKET = "source"
_IDENTITY_KEY_ENV = "PROXBOX_AUTH_LOCKOUT_HMAC_KEY"
_IDENTITY_KEY_FILE_ENV = "PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE"
_MIN_RESERVATION_LEASE_SECONDS = 60
_lockout_table = cast(Any, AuthLockout).__table__
_metrics_table = cast(Any, AuthLockoutMetric).__table__

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
        return cls(
            threshold=threshold,
            window_seconds=window_seconds,
            source_threshold=source_threshold,
            max_buckets=max_buckets,
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


@lru_cache(maxsize=1)
def _identity_hmac_key() -> bytes:
    """Return a cross-worker secret key for opaque credential identities."""

    configured = os.environ.get(_IDENTITY_KEY_ENV, "").strip()
    if configured:
        if len(configured.encode("utf-8")) < 32:
            raise LockoutConfigurationError(f"{_IDENTITY_KEY_ENV} must be at least 32 bytes")
        return hashlib.sha256(configured.encode("utf-8")).digest()

    override = os.environ.get(_IDENTITY_KEY_FILE_ENV, "").strip()
    if override:
        key_path = Path(override).expanduser()
    else:
        database_path = database.sqlite_file_name
        if database_path is None:
            raise LockoutConfigurationError(
                "the database must be initialized before deriving the default "
                "PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE path"
            )
        key_path = database_path.with_name(f"{database_path.name}.auth-lockout.key")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = key_path.with_name(f"{key_path.name}.lock")
    try:
        lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise LockoutConfigurationError(
            f"unable to initialize {_IDENTITY_KEY_FILE_ENV}: {key_path}"
        ) from exc
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        if not key_path.exists():
            descriptor = os.open(
                key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(descriptor, secrets.token_urlsafe(48).encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if key_path.stat().st_mode & 0o077:
            raise LockoutConfigurationError(
                f"{_IDENTITY_KEY_FILE_ENV} must not be accessible by group or other users"
            )
        configured = key_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LockoutConfigurationError(
            f"unable to read {_IDENTITY_KEY_FILE_ENV}: {key_path}"
        ) from exc
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)
    if len(configured.encode("utf-8")) < 32:
        raise LockoutConfigurationError(f"{_IDENTITY_KEY_FILE_ENV} must contain at least 32 bytes")
    return hashlib.sha256(configured.encode("utf-8")).digest()


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
    expired = now >= columns.window_started_at + window_seconds
    stale_reservation = (
        (columns.in_flight > 0)
        & columns.reservation_expires_at.is_not(None)
        & (columns.reservation_expires_at <= now)
    )
    expired_and_reclaimable = cast(Any, expired) & ((columns.in_flight == 0) | stale_reservation)
    available = or_(cast(Any, ~expired), expired_and_reclaimable)
    next_attempts = case((expired_and_reclaimable, 1), else_=columns.attempts + 1)
    next_window = case((expired_and_reclaimable, now), else_=columns.window_started_at)
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
            in_flight=0,
            reservation_tokens="",
            reservation_expires_at=None,
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
                "in_flight": case((stale_reservation, 0), else_=columns.in_flight),
                "reservation_tokens": case(
                    (stale_reservation, ""), else_=columns.reservation_tokens
                ),
                "reservation_expires_at": case(
                    (stale_reservation, None), else_=columns.reservation_expires_at
                ),
                "window_started_at": next_window,
                "locked_until": next_locked_until,
                "updated_at": now,
            },
            where=available,
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
    bucket_id: str,
    bucket_type: str,
    source_context: str,
    credential_id: str,
    threshold: int,
    window_seconds: int,
    lease_seconds: int,
    reservation_token: str,
    now: float,
):
    """Atomically reserve one verification slot without holding a lock during bcrypt."""

    columns = _lockout_table.c
    expired = now >= columns.window_started_at + window_seconds
    stale_reservation = (
        (columns.in_flight > 0)
        & columns.reservation_expires_at.is_not(None)
        & (columns.reservation_expires_at <= now)
    )
    expired_and_idle = cast(Any, expired) & ((columns.in_flight == 0) | stale_reservation)
    within_window = cast(Any, ~expired) & (columns.attempts < threshold)
    available = or_(expired_and_idle, within_window)
    next_attempts = case((expired_and_idle, 1), else_=columns.attempts + 1)
    next_window = case((expired_and_idle, now), else_=columns.window_started_at)
    reset_reservations = expired_and_idle | stale_reservation
    next_in_flight = case((reset_reservations, 1), else_=columns.in_flight + 1)
    next_tokens = case(
        (reset_reservations, reservation_token),
        (columns.reservation_tokens == "", reservation_token),
        else_=columns.reservation_tokens + "," + reservation_token,
    )
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
            attempts=1,
            in_flight=1,
            reservation_tokens=reservation_token,
            reservation_expires_at=now + lease_seconds,
            window_started_at=now,
            locked_until=now + window_seconds if threshold == 1 else None,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[columns.bucket_id],
            set_={
                "bucket_type": bucket_type,
                "source_context": source_context,
                "credential_id": credential_id,
                "attempts": next_attempts,
                "in_flight": next_in_flight,
                "reservation_tokens": next_tokens,
                "reservation_expires_at": now + lease_seconds,
                "window_started_at": next_window,
                "locked_until": next_locked_until,
                "updated_at": now,
            },
            where=available,
        )
        .returning(
            columns.attempts,
            columns.window_started_at,
            columns.locked_until,
            columns.updated_at,
        )
    )


def _release_reservation_statement(
    *,
    bucket_id: str,
    threshold: int,
    reservation_token: str,
    succeeded: bool,
    now: float,
):
    columns = _lockout_table.c
    next_attempts = columns.attempts - 1 if succeeded else columns.attempts
    token_present = func.instr(columns.reservation_tokens, reservation_token) > 0
    remaining_tokens = func.trim(
        func.replace(
            func.replace(columns.reservation_tokens, reservation_token, ""),
            ",,",
            ",",
        ),
        ",",
    )
    return (
        update(_lockout_table)
        .where(columns.bucket_id == bucket_id, columns.in_flight > 0, token_present)
        .values(
            attempts=next_attempts,
            in_flight=columns.in_flight - 1,
            reservation_tokens=remaining_tokens,
            reservation_expires_at=case(
                (columns.in_flight - 1 <= 0, None),
                else_=columns.reservation_expires_at,
            ),
            locked_until=case(
                (next_attempts >= threshold, columns.locked_until),
                else_=None,
            ),
            updated_at=now,
        )
        .returning(columns.bucket_id)
    )


def _prune_expired_statement(policy: AuthLockoutPolicy, now: float):
    """Bound transient bucket growth using the configured inactivity window."""

    return delete(_lockout_table).where(
        _lockout_table.c.updated_at < now - policy.window_seconds,
        or_(
            _lockout_table.c.in_flight == 0,
            _lockout_table.c.reservation_expires_at <= now,
        ),
    )


def _record_metrics_statement(
    *,
    now: float,
    failure: int = 0,
    lockout: int = 0,
    source_lockout: int = 0,
    recovery: int = 0,
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
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[columns.id],
            set_={
                "failures_total": columns.failures_total + failure,
                "lockouts_total": columns.lockouts_total + lockout,
                "source_lockouts_total": columns.source_lockouts_total + source_lockout,
                "recoveries_total": columns.recoveries_total + recovery,
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


def _cap_victim_statement(identity: LockoutIdentity, count: int, now: float):
    """Choose inactive rows while retaining active lockouts and reservations."""

    protected = (identity.bucket_id, identity.source_bucket_id)
    credential_first = case(
        (_lockout_table.c.bucket_type == _CREDENTIAL_BUCKET, 0),
        else_=1,
    )
    return (
        select(_lockout_table.c.bucket_id)
        .where(
            _lockout_table.c.bucket_id.not_in(protected),
            or_(
                _lockout_table.c.in_flight == 0,
                _lockout_table.c.reservation_expires_at <= now,
            ),
            or_(
                _lockout_table.c.locked_until.is_(None),
                _lockout_table.c.locked_until <= now,
            ),
        )
        .order_by(credential_first, _lockout_table.c.updated_at, _lockout_table.c.bucket_id)
        .limit(count)
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
        """Atomically admit one bcrypt verification within both durable budgets."""

        timestamp = time.time() if now is None else now
        reservation_token = secrets.token_hex(16)
        lease_seconds = max(policy.window_seconds, _MIN_RESERVATION_LEASE_SECONDS)
        session.exec(cast(Any, _prune_expired_statement(policy, timestamp)))
        credential_row = session.exec(
            cast(
                Any,
                _reserve_verification_statement(
                    bucket_id=identity.bucket_id,
                    bucket_type=_CREDENTIAL_BUCKET,
                    source_context=identity.source_context,
                    credential_id=identity.credential_id,
                    threshold=policy.threshold,
                    window_seconds=policy.window_seconds,
                    lease_seconds=lease_seconds,
                    reservation_token=reservation_token,
                    now=timestamp,
                ),
            )
        ).first()
        if credential_row is None:
            session.rollback()
            return None
        source_row = session.exec(
            cast(
                Any,
                _reserve_verification_statement(
                    bucket_id=identity.source_bucket_id,
                    bucket_type=_SOURCE_BUCKET,
                    source_context=identity.source_context,
                    credential_id="",
                    threshold=policy.source_threshold,
                    window_seconds=policy.window_seconds,
                    lease_seconds=lease_seconds,
                    reservation_token=reservation_token,
                    now=timestamp,
                ),
            )
        ).first()
        if source_row is None or not AuthLockoutService._enforce_cap(
            session, identity, policy.max_buckets, timestamp
        ):
            session.rollback()
            return None
        session.commit()
        return FailureResult(
            credential=_state_from_row(cast(Sequence[Any], credential_row)),
            source=_state_from_row(cast(Sequence[Any], source_row)),
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
        await session.exec(cast(Any, _prune_expired_statement(policy, timestamp)))
        credential_row = (
            await session.exec(
                cast(
                    Any,
                    _reserve_verification_statement(
                        bucket_id=identity.bucket_id,
                        bucket_type=_CREDENTIAL_BUCKET,
                        source_context=identity.source_context,
                        credential_id=identity.credential_id,
                        threshold=policy.threshold,
                        window_seconds=policy.window_seconds,
                        lease_seconds=lease_seconds,
                        reservation_token=reservation_token,
                        now=timestamp,
                    ),
                )
            )
        ).first()
        if credential_row is None:
            await session.rollback()
            return None
        source_row = (
            await session.exec(
                cast(
                    Any,
                    _reserve_verification_statement(
                        bucket_id=identity.source_bucket_id,
                        bucket_type=_SOURCE_BUCKET,
                        source_context=identity.source_context,
                        credential_id="",
                        threshold=policy.source_threshold,
                        window_seconds=policy.window_seconds,
                        lease_seconds=lease_seconds,
                        reservation_token=reservation_token,
                        now=timestamp,
                    ),
                )
            )
        ).first()
        if source_row is None or not await AuthLockoutService._enforce_cap_async(
            session, identity, policy.max_buckets, timestamp
        ):
            await session.rollback()
            return None
        await session.commit()
        return FailureResult(
            credential=_state_from_row(cast(Sequence[Any], credential_row)),
            source=_state_from_row(cast(Sequence[Any], source_row)),
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
    ) -> None:
        """Release an admission; retain its attempt only when verification failed."""

        timestamp = time.time() if now is None else now
        reservation_token = reservation.reservation_token
        if reservation_token is None:
            raise ValueError("verification reservation has no token")
        credential_release = session.exec(
            cast(
                Any,
                _release_reservation_statement(
                    bucket_id=identity.bucket_id,
                    threshold=policy.threshold,
                    reservation_token=reservation_token,
                    succeeded=succeeded,
                    now=timestamp,
                ),
            )
        ).first()
        source_release = session.exec(
            cast(
                Any,
                _release_reservation_statement(
                    bucket_id=identity.source_bucket_id,
                    threshold=policy.source_threshold,
                    reservation_token=reservation_token,
                    succeeded=succeeded,
                    now=timestamp,
                ),
            )
        ).first()
        if credential_release is None or source_release is None:
            session.rollback()
            return
        if not succeeded:
            session.exec(
                cast(
                    Any,
                    _record_metrics_statement(
                        now=timestamp,
                        failure=1,
                        lockout=int(reservation.credential.attempts == policy.threshold),
                        source_lockout=int(reservation.source.attempts == policy.source_threshold),
                    ),
                )
            )
        session.commit()

    @staticmethod
    async def finalize_verification_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        reservation: FailureResult,
        *,
        succeeded: bool,
        now: float | None = None,
    ) -> None:
        """Async counterpart to :meth:`finalize_verification`."""

        timestamp = time.time() if now is None else now
        reservation_token = reservation.reservation_token
        if reservation_token is None:
            raise ValueError("verification reservation has no token")
        credential_release = (
            await session.exec(
                cast(
                    Any,
                    _release_reservation_statement(
                        bucket_id=identity.bucket_id,
                        threshold=policy.threshold,
                        reservation_token=reservation_token,
                        succeeded=succeeded,
                        now=timestamp,
                    ),
                )
            )
        ).first()
        source_release = (
            await session.exec(
                cast(
                    Any,
                    _release_reservation_statement(
                        bucket_id=identity.source_bucket_id,
                        threshold=policy.source_threshold,
                        reservation_token=reservation_token,
                        succeeded=succeeded,
                        now=timestamp,
                    ),
                )
            )
        ).first()
        if credential_release is None or source_release is None:
            await session.rollback()
            return
        if not succeeded:
            await session.exec(
                cast(
                    Any,
                    _record_metrics_statement(
                        now=timestamp,
                        failure=1,
                        lockout=int(reservation.credential.attempts == policy.threshold),
                        source_lockout=int(reservation.source.attempts == policy.source_threshold),
                    ),
                )
            )
        await session.commit()

    @staticmethod
    def record_failure(
        session: Session,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float | None = None,
    ) -> FailureResult:
        timestamp = time.time() if now is None else now
        session.exec(cast(Any, _prune_expired_statement(policy, timestamp)))
        credential_row = session.exec(
            cast(
                Any,
                _record_failure_statement(
                    bucket_id=identity.bucket_id,
                    bucket_type=_CREDENTIAL_BUCKET,
                    source_context=identity.source_context,
                    credential_id=identity.credential_id,
                    threshold=policy.threshold,
                    window_seconds=policy.window_seconds,
                    now=timestamp,
                ),
            )
        ).first()
        if credential_row is None:
            session.rollback()
            raise LockoutCapacityError("authentication verification is still in progress")
        source_row = session.exec(
            cast(
                Any,
                _record_failure_statement(
                    bucket_id=identity.source_bucket_id,
                    bucket_type=_SOURCE_BUCKET,
                    source_context=identity.source_context,
                    credential_id="",
                    threshold=policy.source_threshold,
                    window_seconds=policy.window_seconds,
                    now=timestamp,
                ),
            )
        ).first()
        if source_row is None:
            session.rollback()
            raise LockoutCapacityError("authentication verification is still in progress")
        credential_state = _state_from_row(cast(Sequence[Any], credential_row))
        source_state = _state_from_row(cast(Sequence[Any], source_row))
        if not AuthLockoutService._enforce_cap(session, identity, policy.max_buckets, timestamp):
            session.rollback()
            raise LockoutCapacityError("authentication lockout capacity is exhausted")
        entered_lockout = credential_state.attempts == policy.threshold
        entered_source_lockout = source_state.attempts == policy.source_threshold
        session.exec(
            cast(
                Any,
                _record_metrics_statement(
                    now=timestamp,
                    failure=1,
                    lockout=int(entered_lockout),
                    source_lockout=int(entered_source_lockout),
                ),
            )
        )
        session.commit()
        if entered_lockout:
            logger.warning(
                "Authentication credential bucket entered lockout",
                extra={
                    "auth_bucket_id": identity.safe_id,
                    "auth_source_context": identity.source_context,
                    "auth_credential_id": identity.credential_id,
                },
            )
        if entered_source_lockout:
            logger.warning(
                "Authentication source budget entered lockout",
                extra={
                    "auth_bucket_id": identity.source_bucket_id[:_SAFE_IDENTIFIER_LENGTH],
                    "auth_source_context": identity.source_context,
                },
            )
        return FailureResult(credential=credential_state, source=source_state)

    @staticmethod
    async def record_failure_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        policy: AuthLockoutPolicy,
        now: float | None = None,
    ) -> FailureResult:
        timestamp = time.time() if now is None else now
        await session.exec(cast(Any, _prune_expired_statement(policy, timestamp)))
        credential_row = (
            await session.exec(
                cast(
                    Any,
                    _record_failure_statement(
                        bucket_id=identity.bucket_id,
                        bucket_type=_CREDENTIAL_BUCKET,
                        source_context=identity.source_context,
                        credential_id=identity.credential_id,
                        threshold=policy.threshold,
                        window_seconds=policy.window_seconds,
                        now=timestamp,
                    ),
                )
            )
        ).first()
        if credential_row is None:
            await session.rollback()
            raise LockoutCapacityError("authentication verification is still in progress")
        source_row = (
            await session.exec(
                cast(
                    Any,
                    _record_failure_statement(
                        bucket_id=identity.source_bucket_id,
                        bucket_type=_SOURCE_BUCKET,
                        source_context=identity.source_context,
                        credential_id="",
                        threshold=policy.source_threshold,
                        window_seconds=policy.window_seconds,
                        now=timestamp,
                    ),
                )
            )
        ).first()
        if source_row is None:
            await session.rollback()
            raise LockoutCapacityError("authentication verification is still in progress")
        credential_state = _state_from_row(cast(Sequence[Any], credential_row))
        source_state = _state_from_row(cast(Sequence[Any], source_row))
        if not await AuthLockoutService._enforce_cap_async(
            session, identity, policy.max_buckets, timestamp
        ):
            await session.rollback()
            raise LockoutCapacityError("authentication lockout capacity is exhausted")
        entered_lockout = credential_state.attempts == policy.threshold
        entered_source_lockout = source_state.attempts == policy.source_threshold
        await session.exec(
            cast(
                Any,
                _record_metrics_statement(
                    now=timestamp,
                    failure=1,
                    lockout=int(entered_lockout),
                    source_lockout=int(entered_source_lockout),
                ),
            )
        )
        await session.commit()
        if entered_lockout:
            logger.warning(
                "Authentication credential bucket entered lockout",
                extra={
                    "auth_bucket_id": identity.safe_id,
                    "auth_source_context": identity.source_context,
                    "auth_credential_id": identity.credential_id,
                },
            )
        if entered_source_lockout:
            logger.warning(
                "Authentication source budget entered lockout",
                extra={
                    "auth_bucket_id": identity.source_bucket_id[:_SAFE_IDENTIFIER_LENGTH],
                    "auth_source_context": identity.source_context,
                },
            )
        return FailureResult(credential=credential_state, source=source_state)

    @staticmethod
    def _enforce_cap(
        session: Session,
        identity: LockoutIdentity,
        max_buckets: int,
        now: float,
    ) -> bool:
        count = int(session.exec(select(func.count()).select_from(_lockout_table)).one())
        excess = count - max_buckets
        if excess <= 0:
            return True
        victims = _cap_victim_statement(identity, excess, now)
        session.exec(
            cast(Any, delete(_lockout_table).where(_lockout_table.c.bucket_id.in_(victims)))
        )
        remaining = int(session.exec(select(func.count()).select_from(_lockout_table)).one())
        return remaining <= max_buckets

    @staticmethod
    async def _enforce_cap_async(
        session: AsyncSession,
        identity: LockoutIdentity,
        max_buckets: int,
        now: float,
    ) -> bool:
        count = int((await session.exec(select(func.count()).select_from(_lockout_table))).one())
        excess = count - max_buckets
        if excess <= 0:
            return True
        victims = _cap_victim_statement(identity, excess, now)
        await session.exec(
            cast(Any, delete(_lockout_table).where(_lockout_table.c.bucket_id.in_(victims)))
        )
        remaining = int(
            (await session.exec(select(func.count()).select_from(_lockout_table))).one()
        )
        return remaining <= max_buckets

    @staticmethod
    def clear(session: Session, identity: LockoutIdentity) -> bool:
        row = AuthLockoutService.get(session, identity)
        if row is None:
            session.rollback()
            return False
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
        session.delete(matches[0])
        session.exec(cast(Any, _record_metrics_statement(now=time.time(), recovery=1)))
        session.commit()
        return 1

    @staticmethod
    def clear_all(session: Session) -> int:
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
    counters = session.get(AuthLockoutMetric, 1)
    return {
        "proxbox_auth_failures_total": counters.failures_total if counters else 0,
        "proxbox_auth_lockouts_total": counters.lockouts_total if counters else 0,
        "proxbox_auth_source_lockouts_total": counters.source_lockouts_total if counters else 0,
        "proxbox_auth_recoveries_total": counters.recoveries_total if counters else 0,
        "proxbox_auth_active_lockouts": int(active_credentials),
        "proxbox_auth_active_source_lockouts": int(active_sources),
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
        "# HELP proxbox_auth_active_lockouts Current locked credential buckets",
        "# TYPE proxbox_auth_active_lockouts gauge",
        f"proxbox_auth_active_lockouts {metrics['proxbox_auth_active_lockouts']}",
        "# HELP proxbox_auth_active_source_lockouts Current locked source budgets",
        "# TYPE proxbox_auth_active_source_lockouts gauge",
        f"proxbox_auth_active_source_lockouts {metrics['proxbox_auth_active_source_lockouts']}",
    ]
    return "\n".join(lines) + "\n"
