"""SQLModel database configuration and endpoint models."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import sqlite3
import stat
import threading
import time
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, cast
from uuid import uuid4

import bcrypt
from fastapi import Depends
from sqlalchemy import JSON, CheckConstraint, Column, event, func, inspect, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import Field, Session, SQLModel, create_engine, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.constants import DEFAULT_DB_PATH
from proxbox_api.credentials import decrypt_value, encrypt_value

_SUPPORTED_SQLITE_DRIVERS = frozenset({"sqlite", "sqlite+pysqlite", "sqlite+aiosqlite"})
_WRITABLE_MODE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
_SEARCHABLE_MODE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
_FRESH_DATABASE_OVERRIDE = "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY"
logger = logging.getLogger(__name__)


class DatabaseConfigurationError(ValueError):
    """The operator supplied an invalid or ambiguous database target."""


class DatabaseStartupError(RuntimeError):
    """The configured SQLite target cannot safely serve the application."""


class DatabaseNotInitializedError(RuntimeError):
    """Database services were requested before application startup."""


class DatabaseConfigurationSource(StrEnum):
    """Configuration source used to select the SQLite file."""

    DEFAULT = "default"
    PROXBOX_DATABASE_PATH = "PROXBOX_DATABASE_PATH"
    DATABASE_URL = "DATABASE_URL"
    MATCHING_ENVIRONMENT = "matching_environment"


@dataclass(frozen=True, slots=True)
class SQLiteDatabaseTarget:
    """Canonical SQLite target resolved before engine construction."""

    path: Path
    source: DatabaseConfigurationSource
    fresh_database_override: bool = False
    legacy_database_paths: tuple[Path, ...] = ()

    @property
    def startup_lock_path(self) -> Path:
        """Return the persistent sibling lock that serializes startup DDL."""
        return self.path.with_name(f"{self.path.name}.startup.lock")

    @property
    def runtime_lock_path(self) -> Path:
        """Return the sibling lock proving that no backend worker is active."""

        return self.path.with_name(f"{self.path.name}.runtime.lock")

    @property
    def fresh_database_override_marker_path(self) -> Path:
        """Return the durable marker that prevents reuse of a one-start override."""
        return self.path.with_name(f"{self.path.name}.fresh-database-override-used")

    @property
    def sync_engine_url(self) -> URL:
        """Return a structured URL so path delimiters are never reparsed."""
        return URL.create("sqlite", database=str(self.path))

    @property
    def async_engine_url(self) -> URL:
        """Return the structured aiosqlite URL for engine construction."""
        return URL.create("sqlite+aiosqlite", database=str(self.path))

    @property
    def sync_url(self) -> str:
        """Return the normalized synchronous SQLAlchemy URL."""
        return self.sync_engine_url.render_as_string(hide_password=False)

    @property
    def async_url(self) -> str:
        """Return the normalized aiosqlite SQLAlchemy URL."""
        return self.async_engine_url.render_as_string(hide_password=False)


database_target: SQLiteDatabaseTarget | None = None
# Legacy public module names remain available, but are intentionally unset
# until lifespan startup resolves and verifies the configured target.
sqlite_file_name: Path | None = None
sqlite_url: str | None = None
async_sqlite_url: str | None = None
engine: Engine | None = None
async_engine: AsyncEngine | None = None
async_session_factory: async_sessionmaker[AsyncSession] | None = None
connect_args = {"check_same_thread": False}
_database_runtime_lock = threading.RLock()
_database_runtime_lease_descriptor: int | None = None


def _absolute_database_path(raw_path: str, *, variable: str) -> Path:
    """Normalize one configured path without falling back to the process cwd."""
    if "\x00" in raw_path:
        raise DatabaseConfigurationError(f"{variable} contains an invalid path value.")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise DatabaseConfigurationError(
            f"{variable} must select an absolute SQLite file path; relative paths are refused."
        )
    return path.resolve(strict=False)


def _path_from_database_url(raw_url: str) -> Path:
    """Parse a SQLite DATABASE_URL without exposing its raw value in errors."""
    if "?" in raw_url:
        raise DatabaseConfigurationError(
            "DATABASE_URL query parameters or query delimiters are not supported "
            "for the operational database."
        )
    try:
        url = make_url(raw_url)
    except ArgumentError as error:
        raise DatabaseConfigurationError(
            "DATABASE_URL must be a valid absolute SQLite URL."
        ) from error

    if url.drivername not in _SUPPORTED_SQLITE_DRIVERS:
        raise DatabaseConfigurationError(
            "DATABASE_URL must use sqlite, sqlite+pysqlite, or sqlite+aiosqlite."
        )
    if any((url.username, url.password, url.host, url.port)):
        raise DatabaseConfigurationError(
            "DATABASE_URL must identify a local SQLite file without authority or credentials."
        )
    if url.query:
        raise DatabaseConfigurationError(
            "DATABASE_URL query parameters are not supported for the operational database."
        )
    if not url.database or url.database == ":memory:":
        raise DatabaseConfigurationError(
            "DATABASE_URL must identify a persistent absolute SQLite file."
        )
    return _absolute_database_path(url.database, variable="DATABASE_URL")


def _default_database_path(environment: Mapping[str, str]) -> Path:
    """Return the absolute packaged/user-data fallback without consulting cwd."""
    packaged_default = environment.get("PROXBOX_DEFAULT_DATABASE_PATH", "").strip()
    if packaged_default:
        return _absolute_database_path(
            packaged_default,
            variable="PROXBOX_DEFAULT_DATABASE_PATH",
        )

    xdg_data_home = environment.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        base = _absolute_database_path(xdg_data_home, variable="XDG_DATA_HOME")
        return base / "proxbox" / "database.db"

    home = environment.get("HOME", "").strip()
    if home:
        base = _absolute_database_path(home, variable="HOME")
        return base / ".local" / "share" / "proxbox" / "database.db"

    return _absolute_database_path(DEFAULT_DB_PATH, variable="default database path")


def _legacy_default_database_candidates() -> tuple[Path, Path]:
    """Return file locations selected implicitly by older releases."""
    return Path("/data/database.db"), Path.cwd() / "database.db"


def _legacy_database_paths(selected_path: Path) -> tuple[Path, ...]:
    """Return existing legacy targets other than the selected canonical file."""
    selected_path = selected_path.resolve(strict=False)
    conflicts: list[Path] = []
    for legacy_path in _legacy_default_database_candidates():
        try:
            legacy_path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise DatabaseConfigurationError(
                f"Cannot verify legacy SQLite candidate {legacy_path}. "
                "Correct its parent-directory permissions or filesystem state."
            ) from error
        try:
            resolved_legacy_path = legacy_path.resolve(strict=True)
            resolved_legacy_path.stat()
        except OSError as error:
            raise DatabaseConfigurationError(
                f"Cannot verify legacy SQLite candidate {legacy_path}. "
                "Correct a broken link, permissions, or filesystem state."
            ) from error
        if resolved_legacy_path != selected_path:
            conflicts.append(resolved_legacy_path)
    return tuple(dict.fromkeys(conflicts))


def _target_preserves_api_key_bootstrap(path: Path) -> bool:
    """Return whether an existing target contains durable API-key history.

    Claim evidence must match the runtime contract exactly: the singleton row
    is ``id = 1`` and its ORM columns must exist. Malformed evidence is fatal;
    it must never become permission to serve a copied database whose public
    first-key registration route still considers bootstrap unclaimed.
    """
    connection: sqlite3.Connection | None = None
    try:
        if not path.exists() or not path.is_file() or path.stat().st_size == 0:
            return False
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "api_key_bootstrap_claim" in tables:
            claim_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(api_key_bootstrap_claim)"
                ).fetchall()
            }
            if not {"id", "initialized_at"} <= claim_columns:
                raise DatabaseConfigurationError(
                    "The selected SQLite database has an incompatible API-key "
                    "bootstrap-claim schema. Restore or migrate it before startup."
                )
            if connection.execute(
                "SELECT 1 FROM api_key_bootstrap_claim WHERE id != 1 LIMIT 1"
            ).fetchone():
                raise DatabaseConfigurationError(
                    "The selected SQLite database contains a noncanonical API-key "
                    "bootstrap claim. Only the permanent singleton claim id=1 is valid."
                )
            if connection.execute(
                "SELECT 1 FROM api_key_bootstrap_claim WHERE id = 1 LIMIT 1"
            ).fetchone():
                return True
        return "apikey" in tables and bool(
            connection.execute("SELECT 1 FROM apikey LIMIT 1").fetchone()
        )
    except DatabaseConfigurationError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise DatabaseConfigurationError(
            "The selected SQLite database's API-key bootstrap history cannot be "
            "verified. Restore or repair it before startup."
        ) from error
    finally:
        if connection is not None:
            connection.close()


def _fresh_database_override_enabled(environment: Mapping[str, str]) -> bool:
    """Parse the dedicated legacy-bypass override without permissive coercion."""
    raw_value = environment.get(_FRESH_DATABASE_OVERRIDE, "")
    if raw_value in {"", "0"}:
        return False
    if raw_value != "1":
        raise DatabaseConfigurationError(
            f"{_FRESH_DATABASE_OVERRIDE} accepts only 1 for an explicit controlled override."
        )
    return True


def _fresh_override_marker_exists(target: SQLiteDatabaseTarget) -> bool:
    """Check the one-start marker without hiding permission or I/O failures."""
    marker_path = target.fresh_database_override_marker_path
    try:
        marker_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise DatabaseConfigurationError(
            f"Cannot verify fresh-database override marker {marker_path}. "
            "Correct directory permissions or filesystem state."
        ) from error
    return True


def _require_single_worker_fresh_override(environment: Mapping[str, str]) -> None:
    """Require an explicitly single-worker recovery launch for the override."""
    if environment.get("UVICORN_WORKERS") != "1":
        raise DatabaseConfigurationError(
            f"{_FRESH_DATABASE_OVERRIDE}=1 requires UVICORN_WORKERS=1 for the "
            "controlled recovery launch. Stop all workers and start exactly one."
        )
    for variable in ("WEB_CONCURRENCY", "GRANIAN_WORKERS"):
        configured = environment.get(variable)
        if configured is not None and configured != "1":
            raise DatabaseConfigurationError(
                f"{_FRESH_DATABASE_OVERRIDE}=1 requires {variable}=1 or that variable "
                "to be unset for the controlled single-worker recovery launch."
            )


def _apply_legacy_database_guard(
    target: SQLiteDatabaseTarget,
    environment: Mapping[str, str],
) -> SQLiteDatabaseTarget:
    """Protect every fresh target from bypassing existing legacy auth state."""
    legacy_paths = _legacy_database_paths(target.path)
    override_enabled = _fresh_database_override_enabled(environment)
    if not legacy_paths:
        if override_enabled:
            raise DatabaseConfigurationError(
                f"{_FRESH_DATABASE_OVERRIDE}=1 is set, but no conflicting legacy database "
                "exists. Remove the unnecessary security override."
            )
        return target

    if override_enabled:
        _require_single_worker_fresh_override(environment)

    if override_enabled and _fresh_override_marker_exists(target):
        raise DatabaseConfigurationError(
            f"{_FRESH_DATABASE_OVERRIDE}=1 was already consumed for this target. "
            "Remove the stale override; never delete its durable consumption marker "
            "to reauthorize bootstrap."
        )

    if _target_preserves_api_key_bootstrap(target.path):
        if override_enabled:
            raise DatabaseConfigurationError(
                f"{_FRESH_DATABASE_OVERRIDE}=1 is stale because the selected database "
                "already preserves API-key bootstrap history. Remove the override."
            )
        return replace(target, legacy_database_paths=legacy_paths)

    if not override_enabled:
        raise DatabaseConfigurationError(
            "An existing SQLite database was found at a legacy implicit location. "
            "Set PROXBOX_DATABASE_PATH or DATABASE_URL to that database, migrate its "
            "API-key history into the selected target, or deliberately authorize a fresh "
            f"control plane with {_FRESH_DATABASE_OVERRIDE}=1 for one audited startup. "
            "Refusing to create or initialize an empty database because that could reopen "
            "API-key bootstrap."
        )

    return replace(
        target,
        fresh_database_override=True,
        legacy_database_paths=legacy_paths,
    )


def _audit_fresh_database_override(target: SQLiteDatabaseTarget) -> None:
    """Record the exact target and conflicting legacy state before any write."""
    if not target.fresh_database_override:
        return
    legacy_paths = ",".join(str(path) for path in target.legacy_database_paths)
    logger.warning(
        "Fresh SQLite control-plane override accepted after durable consumption: "
        "override=%s target=%s legacy=%s; remove the override after first-key registration",
        _FRESH_DATABASE_OVERRIDE,
        target.path,
        legacy_paths,
        extra={
            "database_path": str(target.path),
            "legacy_database_paths": [str(path) for path in target.legacy_database_paths],
            "security_override": _FRESH_DATABASE_OVERRIDE,
        },
    )


def _consume_fresh_database_override(target: SQLiteDatabaseTarget) -> None:
    """Atomically consume one target's override before the first database write."""
    if not target.fresh_database_override:
        return
    marker_path = target.fresh_database_override_marker_path
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    directory_descriptor: int | None = None
    try:
        descriptor = os.open(marker_path, flags, 0o600)
        os.fsync(descriptor)
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        directory_descriptor = os.open(marker_path.parent, directory_flags)
        os.fsync(directory_descriptor)
    except FileExistsError as error:
        raise DatabaseConfigurationError(
            f"{_FRESH_DATABASE_OVERRIDE}=1 was already consumed for this target. "
            "Remove the stale override."
        ) from error
    except OSError as error:
        raise DatabaseStartupError(
            f"Cannot persist the fresh-database override consumption marker: {marker_path}. "
            "Check directory ownership, permissions, and available space."
        ) from error
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        if descriptor is not None:
            os.close(descriptor)


def resolve_database_target(
    environ: Mapping[str, str] | None = None,
) -> SQLiteDatabaseTarget:
    """Resolve exactly one deterministic SQLite target from process configuration.

    ``PROXBOX_DATABASE_PATH`` is the canonical path-based setting. A SQLite
    ``DATABASE_URL`` remains supported for existing deployments. Supplying both
    is accepted only when they resolve to the same canonical file; divergent
    targets fail startup instead of selecting one by undocumented precedence.
    """
    environment = os.environ if environ is None else environ
    configured_path = environment.get("PROXBOX_DATABASE_PATH", "").strip()
    configured_url = environment.get("DATABASE_URL", "").strip()

    path_target = (
        _absolute_database_path(configured_path, variable="PROXBOX_DATABASE_PATH")
        if configured_path
        else None
    )
    url_target = _path_from_database_url(configured_url) if configured_url else None

    if path_target is not None and url_target is not None:
        if path_target != url_target:
            raise DatabaseConfigurationError(
                "PROXBOX_DATABASE_PATH and DATABASE_URL select different SQLite files; "
                "remove one setting or make them match."
            )
        target = SQLiteDatabaseTarget(
            path=path_target,
            source=DatabaseConfigurationSource.MATCHING_ENVIRONMENT,
        )
    elif path_target is not None:
        target = SQLiteDatabaseTarget(
            path=path_target,
            source=DatabaseConfigurationSource.PROXBOX_DATABASE_PATH,
        )
    elif url_target is not None:
        target = SQLiteDatabaseTarget(
            path=url_target,
            source=DatabaseConfigurationSource.DATABASE_URL,
        )
    else:
        target = SQLiteDatabaseTarget(
            path=_default_database_path(environment),
            source=DatabaseConfigurationSource.DEFAULT,
        )
    return _apply_legacy_database_guard(target, environment)


def _validate_mode_bits(path: Path, *, directory: bool) -> None:
    """Reject targets whose Unix mode is unambiguously read-only.

    The subsequent SQLite transaction remains authoritative. This explicit
    check makes read-only mounts fail consistently even when tests or emergency
    tooling happen to run as root, which can otherwise bypass ordinary mode
    checks.
    """
    mode = path.stat().st_mode
    if mode & _WRITABLE_MODE_BITS == 0:
        kind = "directory" if directory else "file"
        raise DatabaseStartupError(
            f"Configured SQLite {kind} is read-only: {path}. "
            "Grant write access to the proxbox-api service account."
        )
    if directory and mode & _SEARCHABLE_MODE_BITS == 0:
        raise DatabaseStartupError(
            f"Configured SQLite directory is not searchable: {path}. "
            "Grant directory execute access to the proxbox-api service account."
        )


def _prepare_sqlite_path(path: Path) -> None:
    """Create and validate only the parent selected by configuration."""
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DatabaseStartupError(
            f"Cannot create configured SQLite directory: {parent}. "
            "Create it and grant ownership to the proxbox-api service account."
        ) from error

    if not parent.is_dir():
        raise DatabaseStartupError(f"Configured SQLite parent is not a directory: {parent}.")
    _validate_mode_bits(parent, directory=True)
    if path.exists():
        if not path.is_file():
            raise DatabaseStartupError(f"Configured SQLite target is not a file: {path}.")
        _validate_mode_bits(path, directory=False)


@contextmanager
def _database_startup_advisory_lock(
    target: SQLiteDatabaseTarget,
) -> Generator[Path, None, None]:
    """Serialize one target's complete probe and schema-migration boundary."""
    _prepare_sqlite_path(target.path)
    lock_path = target.startup_lock_path
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise DatabaseStartupError(f"SQLite startup lock is not a regular file: {lock_path}.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except DatabaseStartupError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise DatabaseStartupError(
            f"Cannot acquire the SQLite startup lock beside the configured database: "
            f"{lock_path}. Check directory ownership and permissions."
        ) from error

    assert descriptor is not None
    try:
        yield lock_path
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _open_database_lock(path: Path) -> int:
    """Open one private regular lock file without following symlinks."""

    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise DatabaseStartupError(f"SQLite lock is not a regular file: {path}.")
    return descriptor


def _acquire_runtime_database_lease(target: SQLiteDatabaseTarget) -> None:
    """Hold a shared lease for this process until database disposal."""

    global _database_runtime_lease_descriptor

    if _database_runtime_lease_descriptor is not None:
        return
    descriptor: int | None = None
    try:
        descriptor = _open_database_lock(target.runtime_lock_path)
        fcntl.flock(descriptor, fcntl.LOCK_SH)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise
    _database_runtime_lease_descriptor = descriptor


@contextmanager
def offline_database_maintenance_lock(path: Path) -> Generator[None, None, None]:
    """Exclude startup and every live backend worker during offline maintenance."""

    target = SQLiteDatabaseTarget(
        path=path.expanduser().resolve(),
        source=DatabaseConfigurationSource.PROXBOX_DATABASE_PATH,
    )
    with _database_startup_advisory_lock(target):
        descriptor: int | None = None
        try:
            descriptor = _open_database_lock(target.runtime_lock_path)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise DatabaseStartupError(
                "Offline database maintenance was refused because a proxbox-api "
                "worker still holds the runtime lease. Stop every worker first."
            ) from error
        try:
            yield
        finally:
            assert descriptor is not None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _run_sqlite_write_probe(connection: sqlite3.Connection, path: Path) -> None:
    """Prove WAL mode and a rolled-back write against the main database."""
    probe_table = f"__proxbox_startup_write_probe_{uuid4().hex}"
    connection.execute("PRAGMA busy_timeout=5000")
    journal_row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    journal_mode = str(journal_row[0]).lower() if journal_row else ""
    if journal_mode != "wal":
        raise DatabaseStartupError(
            f"Configured SQLite filesystem did not enable WAL mode for {path}; "
            f"reported journal mode was {journal_mode or 'unknown'}."
        )

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(f'CREATE TABLE "{probe_table}" (value INTEGER NOT NULL)')
        connection.execute(f'INSERT INTO "{probe_table}" (value) VALUES (1)')
        value = connection.execute(f'SELECT value FROM "{probe_table}" LIMIT 1').fetchone()
        if value != (1,):
            raise DatabaseStartupError(
                f"Configured SQLite write verification returned invalid data for {path}."
            )
    finally:
        if connection.in_transaction:
            connection.rollback()

    residue = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (probe_table,),
    ).fetchone()
    if residue is not None:
        raise DatabaseStartupError(
            f"Configured SQLite write verification did not roll back cleanly for {path}."
        )


def verify_sqlite_target(target: SQLiteDatabaseTarget) -> None:
    """Create the intended parent and prove SQLite WAL writes are usable.

    The probe creates and writes a uniquely named table inside ``BEGIN
    IMMEDIATE`` and always rolls the transaction back. Production rows and
    schema are never committed, while the operation still exercises the main
    database file plus its WAL/SHM sidecars.
    """
    path = target.path
    _prepare_sqlite_path(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        _run_sqlite_write_probe(connection, path)
    except DatabaseStartupError:
        raise
    except sqlite3.Error as error:
        error_code = getattr(error, "sqlite_errorname", type(error).__name__)
        raise DatabaseStartupError(
            f"Configured SQLite database is not writable with WAL at {path} "
            f"({error_code}). Check file and directory ownership, permissions, and mount mode."
        ) from error
    finally:
        if connection is not None:
            connection.close()


def initialize_database(
    environ: Mapping[str, str] | None = None,
) -> SQLiteDatabaseTarget:
    """Resolve, verify, and construct the operational database engines once."""
    target = resolve_database_target(environ)
    with _database_runtime_lock, _database_startup_advisory_lock(target):
        _consume_fresh_database_override(target)
        _audit_fresh_database_override(target)
        initialized_target = _initialize_database_target(target)
        _acquire_runtime_database_lease(target)
        return initialized_target


def _initialize_database_target(target: SQLiteDatabaseTarget) -> SQLiteDatabaseTarget:
    """Verify and construct engines for an already guarded, locked target."""
    global database_target, sqlite_file_name, sqlite_url, async_sqlite_url
    global engine, async_engine, async_session_factory

    with _database_runtime_lock:
        if database_target is not None:
            if database_target.path != target.path:
                raise DatabaseStartupError(
                    "Database runtime is already initialized with a different SQLite target."
                )
            verify_sqlite_target(database_target)
            return database_target

        verify_sqlite_target(target)
        sync_engine: Engine | None = None
        try:
            sync_engine = create_engine(
                target.sync_engine_url,
                connect_args=connect_args,
                poolclass=NullPool,
            )
            candidate_async_engine = create_async_engine(
                target.async_engine_url,
                connect_args=connect_args,
            )
            if sync_engine.url.database != str(
                target.path
            ) or candidate_async_engine.url.database != str(target.path):
                raise DatabaseStartupError(
                    "Constructed SQLite engines did not preserve the verified database path."
                )
            configure_sqlite_engine(sync_engine)
            configure_sqlite_engine(candidate_async_engine.sync_engine)
            candidate_session_factory = async_sessionmaker(
                candidate_async_engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )
        except Exception as error:  # noqa: BLE001
            if sync_engine is not None:
                sync_engine.dispose()
            raise DatabaseStartupError(
                f"Failed to construct SQLite database engines for {target.path}."
            ) from error

        database_target = target
        sqlite_file_name = target.path
        sqlite_url = target.sync_url
        async_sqlite_url = target.async_url
        engine = sync_engine
        async_engine = candidate_async_engine
        async_session_factory = candidate_session_factory
        return target


def get_engine() -> Engine:
    """Return the initialized synchronous engine or fail clearly."""
    if engine is None:
        raise DatabaseNotInitializedError(
            "Database engine is unavailable before application lifespan startup."
        )
    return engine


def get_async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the initialized async session factory or fail clearly."""
    if async_session_factory is None:
        raise DatabaseNotInitializedError(
            "Async database sessions are unavailable before application lifespan startup."
        )
    return async_session_factory


async def dispose_database() -> None:
    """Dispose process-local engines after lifespan shutdown."""
    global database_target, sqlite_file_name, sqlite_url, async_sqlite_url
    global engine, async_engine, async_session_factory
    global _database_runtime_lease_descriptor

    with _database_runtime_lock:
        sync_engine = engine
        candidate_async_engine = async_engine
        database_target = None
        sqlite_file_name = None
        sqlite_url = None
        async_sqlite_url = None
        engine = None
        async_engine = None
        async_session_factory = None
        runtime_lease_descriptor = _database_runtime_lease_descriptor
        _database_runtime_lease_descriptor = None
    if sync_engine is not None:
        sync_engine.dispose()
    if candidate_async_engine is not None:
        await candidate_async_engine.dispose()
    if runtime_lease_descriptor is not None:
        try:
            fcntl.flock(runtime_lease_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(runtime_lease_descriptor)
    from proxbox_api.services.auth_lockout import clear_runtime_auth_lockout_identity_key

    clear_runtime_auth_lockout_identity_key()


def _apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ARG001
    """Enable WAL journal mode and a 5-second busy timeout on every new connection.

    WAL mode allows concurrent readers alongside a single writer, which prevents
    'database is locked' errors when multiple requests hit the auth-lockout check
    simultaneously.  The busy timeout makes writers wait up to 5 s before raising
    instead of failing immediately.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA journal_mode")
    journal_row = cursor.fetchone()
    if not journal_row or str(journal_row[0]).lower() != "wal":
        cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def configure_sqlite_engine(target_engine: Engine) -> None:
    """Apply the production SQLite concurrency policy to every new connection."""

    if not event.contains(target_engine, "connect", _apply_sqlite_pragmas):
        event.listen(target_engine, "connect", _apply_sqlite_pragmas)


class NetBoxEndpoint(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    ip_address: str = Field(index=True)
    domain: str = Field(index=True)
    port: int = Field(default=443)
    token_version: str = Field(default="v1")
    token_key: str | None = Field(default=None)
    token: str = Field()
    verify_ssl: bool = Field(default=True)
    enabled: bool = Field(default=True)

    @property
    def url(self) -> str:
        protocol = "https" if self.port == 443 or self.verify_ssl else "http"
        host = self.domain if self.domain else self.ip_address.split("/")[0]
        return f"{protocol}://{host}:{self.port}"

    def get_decrypted_token(self) -> str:
        return decrypt_value(self.token) or ""

    def get_decrypted_token_key(self) -> str | None:
        return decrypt_value(self.token_key)

    def set_encrypted_token(self, value: str) -> None:
        self.token = encrypt_value(value) or value

    def set_encrypted_token_key(self, value: str | None) -> None:
        self.token_key = encrypt_value(value)


class ProxmoxEndpoint(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    ip_address: str = Field(index=True)
    domain: str | None = Field(default=None, index=True)
    port: int = Field(default=8006)
    username: str = Field(index=True)
    password: str | None = Field(default=None)
    verify_ssl: bool = Field(default=True)
    # WHY: deliberate safety invariant; agents must not flip this True autonomously. See AGENTS.md section "LLM Agent Safety Guardrails".
    allow_writes: bool = Field(default=False)
    # Narrow, default-off authorization for netbox-packer cloud-init template
    # image creation. This never grants a write by itself: allow_writes remains
    # the broader endpoint gate and both must be true at execution time.
    allow_packer_template_builds: bool = Field(default=False)
    # Transport access method (orthogonal to allow_writes). Values: "api"
    # (Read+Write over API only) or "api_ssh" (Read+Write over API + SSH). SSH is
    # an optional complement to API; "ssh only" is unrepresentable. NEW rows
    # default to "api" (this ORM default); PRE-EXISTING rows are backfilled to
    # "api_ssh" by _migrate_proxmox_endpoint_columns() so currently-ungated SSH
    # usage is not silently broken on upgrade. See proxbox_api/enum/proxmox.py
    # ProxmoxAccessMethod and the SSH-access gate in routes/ssh_terminal.py.
    access_methods: str = Field(default="api")
    enabled: bool = Field(default=True)
    # Cloud Image Pipeline SSH authority. Execution is refused unless all six
    # fields form one complete endpoint/node binding. Caller-supplied SSH
    # values are assertions only and never become execution authority.
    ssh_target_node: str | None = Field(default=None, index=True)
    ssh_host: str | None = Field(default=None)
    ssh_username: str | None = Field(default=None)
    ssh_port: int = Field(default=22)
    ssh_identity_file: str | None = Field(default=None)
    ssh_known_host_fingerprint: str | None = Field(default=None)
    token_name: str | None = Field(default=None)
    token_value: str | None = Field(default=None)
    timeout: int | None = Field(default=None)
    max_retries: int | None = Field(default=None)
    retry_backoff: float | None = Field(default=None)
    site_id: int | None = Field(default=None)
    site_slug: str | None = Field(default=None)
    site_name: str | None = Field(default=None)
    tenant_id: int | None = Field(default=None)
    tenant_slug: str | None = Field(default=None)
    tenant_name: str | None = Field(default=None)

    @property
    def has_token(self) -> bool:
        return bool(self.token_name and self.token_value)

    @property
    def ssh_enabled(self) -> bool:
        """True when this endpoint permits the SSH transport (``api_ssh``)."""
        return self.access_methods == "api_ssh"

    @property
    def has_cloud_image_ssh_binding(self) -> bool:
        """True only for a complete persisted endpoint/node SSH binding."""

        return bool(
            self.enabled
            and self.ssh_target_node
            and self.ssh_host
            and self.ssh_username
            and self.ssh_identity_file
            and self.ssh_known_host_fingerprint
            and 1 <= self.ssh_port <= 65535
        )

    @property
    def host(self) -> str:
        return self.domain or self.ip_address

    def get_decrypted_password(self) -> str | None:
        return decrypt_value(self.password)

    def get_decrypted_token_value(self) -> str | None:
        return decrypt_value(self.token_value)

    def set_encrypted_password(self, value: str | None) -> None:
        self.password = encrypt_value(value)

    def set_encrypted_token_value(self, value: str | None) -> None:
        self.token_value = encrypt_value(value)


class DeletionRequestRecord(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    endpoint_id: int = Field(index=True)
    vmid: int = Field(index=True)
    node: str = Field(index=True)
    kind: str = Field(index=True)
    state: str = Field(default="pending", index=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class FirewallIntentRequestRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "firewall_intent_request"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    endpoint_id: int = Field(index=True)
    actor: str | None = Field(default=None, index=True)
    action: str = Field(index=True)
    state: str = Field(default="planned", index=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    plan_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class CloudImageBuildOperation(SQLModel, table=True):
    """Secret-free durable journal and exclusive lease for one image build.

    The rendered script, URLs, cloud-init, SSH material, and subprocess output
    are deliberately absent. ``lease_key`` is nullable so completed history can
    coexist with a unique active ``endpoint_id:vmid`` owner.
    """

    __tablename__: ClassVar[str] = "cloud_image_build_operation"
    __table_args__ = {"extend_existing": True}

    id: str = Field(primary_key=True)
    plan_digest: str = Field(index=True)
    recipe_digest: str = Field(index=True)
    endpoint_config_digest: str = Field(index=True)
    endpoint_id: int = Field(index=True)
    target_node: str = Field(index=True)
    vmid: int = Field(index=True)
    provider: str = Field(index=True)
    state: str = Field(default="leased", index=True)
    lease_key: str | None = Field(default=None, index=True, unique=True)
    remote_unit: str = Field()
    plan_expires_at: float = Field()
    lease_expires_at: float = Field()
    attempted: bool = Field(default=False)
    exit_code: int | None = Field(default=None)
    stdout_bytes: int = Field(default=0)
    stderr_bytes: int = Field(default=0)
    stdout_lines: int = Field(default=0)
    stderr_lines: int = Field(default=0)
    verified: bool = Field(default=False)
    recovery_required: bool = Field(default=False)
    cancel_requested: bool = Field(default=False)
    cancellation_succeeded: bool | None = Field(default=None)
    error_code: str | None = Field(default=None, index=True)
    created_at: float = Field(default_factory=time.time, index=True)
    started_at: float | None = Field(default=None)
    finished_at: float | None = Field(default=None)
    updated_at: float = Field(default_factory=time.time, index=True)


class PBSEndpoint(SQLModel, table=True):
    """Proxmox Backup Server (PBS) endpoint record.

    Read-only integration in v1: credentials authorize PBS GET calls only.
    ``allow_writes`` is reserved for a future write surface and stays False.
    """

    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    host: str = Field(index=True)
    port: int = Field(default=8007)
    token_id: str = Field()
    token_secret: str = Field()
    fingerprint: str | None = Field(default=None)
    verify_ssl: bool = Field(default=False)
    allow_writes: bool = Field(default=False)
    enabled: bool = Field(default=True)
    timeout_seconds: int = Field(default=30)
    last_seen_at: float | None = Field(default=None)

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self.port}"

    def get_decrypted_token_secret(self) -> str | None:
        return decrypt_value(self.token_secret)

    def set_encrypted_token_secret(self, value: str) -> None:
        self.token_secret = encrypt_value(value) or value


class PDMEndpoint(SQLModel, table=True):
    """Proxmox Datacenter Manager (PDM) endpoint record.

    Read-only integration in v1: credentials authorize PDM GET calls only.
    ``allow_writes`` is reserved and stays False.
    """

    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    host: str = Field(index=True)
    port: int = Field(default=8443)
    token_id: str = Field()
    token_secret: str = Field()
    fingerprint: str | None = Field(default=None)
    verify_ssl: bool = Field(default=True)
    allow_writes: bool = Field(default=False)
    enabled: bool = Field(default=True)
    timeout_seconds: int = Field(default=30)
    last_seen_at: float | None = Field(default=None)

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self.port}"

    def get_decrypted_token_secret(self) -> str | None:
        return decrypt_value(self.token_secret)

    def set_encrypted_token_secret(self, value: str) -> None:
        self.token_secret = encrypt_value(value) or value


class CephOperationRunRecord(SQLModel, table=True):
    """Persisted Ceph v2 plan/apply/reconcile run state."""

    __tablename__: ClassVar[str] = "ceph_operation_run"
    __table_args__ = {"extend_existing": True}

    id: str = Field(primary_key=True)
    plan_id: str | None = Field(default=None, index=True)
    status: str = Field(default="pending", index=True)
    actor: str | None = Field(default=None, index=True)
    source_branch_schema_id: str | None = Field(default=None, index=True)
    provider: str = Field(index=True)
    request_summary: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    provider_task_refs: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time, index=True)
    warnings: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    errors: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    result_summary: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )


class PrometheusSource(SQLModel, table=True):
    """Prometheus metric source for a Ceph cluster (Ceph v2 #94).

    Stores only connection metadata and an optional encrypted bearer token; no
    time-series data is persisted here. ``cluster_ref`` binds the source to a
    Ceph cluster / object reference so the metrics route can resolve the right
    source for a scope.
    """

    __tablename__: ClassVar[str] = "prometheus_source"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    url: str = Field()
    bearer_token: str | None = Field(default=None)
    credential_ref: str | None = Field(default=None)
    cluster_ref: str | None = Field(default=None, index=True)
    verify_ssl: bool = Field(default=True)
    enabled: bool = Field(default=True)
    timeout_seconds: int = Field(default=15)
    scrape_interval_seconds: int = Field(default=60)
    last_seen_at: float | None = Field(default=None)

    def get_decrypted_bearer_token(self) -> str | None:
        return decrypt_value(self.bearer_token) if self.bearer_token else None

    def set_encrypted_bearer_token(self, value: str | None) -> None:
        self.bearer_token = encrypt_value(value) if value else None


class CephDashboardEndpoint(SQLModel, table=True):
    """Direct Ceph Dashboard API endpoint for Ceph v2 (#98).

    Stores connection metadata plus an encrypted password (or token) used to
    authenticate to the Ceph Manager Dashboard REST API. No Proxmox endpoint is
    required, so this works for external/standalone clusters. ``cluster_ref``
    binds the endpoint to a Ceph cluster / object reference.
    """

    __tablename__: ClassVar[str] = "ceph_dashboard_endpoint"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    base_url: str = Field()
    username: str | None = Field(default=None)
    password: str | None = Field(default=None)
    token: str | None = Field(default=None)
    credential_ref: str | None = Field(default=None)
    cluster_ref: str | None = Field(default=None, index=True)
    api_version: str = Field(default="1.0")
    verify_ssl: bool = Field(default=True)
    enabled: bool = Field(default=True)
    timeout_seconds: int = Field(default=30)
    last_seen_at: float | None = Field(default=None)

    def get_decrypted_password(self) -> str | None:
        return decrypt_value(self.password) if self.password else None

    def set_encrypted_password(self, value: str | None) -> None:
        self.password = encrypt_value(value) if value else None

    def get_decrypted_token(self) -> str | None:
        return decrypt_value(self.token) if self.token else None

    def set_encrypted_token(self, value: str | None) -> None:
        self.token = encrypt_value(value) if value else None


class CephExternalCluster(SQLModel, table=True):
    """External (non-Proxmox) Ceph cluster binding for Ceph v2 (#97).

    Provider-neutral: references an optional Ceph Dashboard endpoint and
    Prometheus source (by id) and carries inline RGW Admin Ops credentials.
    No Proxmox endpoint / node / storage / task fields. ``ceph_version_hint``
    feeds capability detection.
    """

    __tablename__: ClassVar[str] = "ceph_external_cluster"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    cluster_ref: str | None = Field(default=None, index=True)
    ceph_version_hint: str | None = Field(default=None)
    dashboard_endpoint_id: int | None = Field(default=None)
    prometheus_source_id: int | None = Field(default=None)
    rgw_admin_url: str | None = Field(default=None)
    rgw_access_key: str | None = Field(default=None)
    rgw_secret_key: str | None = Field(default=None)
    ssh_host: str | None = Field(default=None)
    ssh_user: str | None = Field(default=None)
    ssh_credential_ref: str | None = Field(default=None)
    verify_ssl: bool = Field(default=True)
    enabled: bool = Field(default=True)
    last_seen_at: float | None = Field(default=None)

    def get_decrypted_rgw_access_key(self) -> str | None:
        return decrypt_value(self.rgw_access_key) if self.rgw_access_key else None

    def set_encrypted_rgw_access_key(self, value: str | None) -> None:
        self.rgw_access_key = encrypt_value(value) if value else None

    def get_decrypted_rgw_secret_key(self) -> str | None:
        return decrypt_value(self.rgw_secret_key) if self.rgw_secret_key else None

    def set_encrypted_rgw_secret_key(self, value: str | None) -> None:
        self.rgw_secret_key = encrypt_value(value) if value else None


class AuthLockout(SQLModel, table=True):
    """Composite, credential-isolated authentication failure bucket."""

    # Keep the legacy ``authlockout`` table intact so an application rollback
    # can still authenticate. Legacy IP-only rows are intentionally ignored by
    # the new implementation because they cannot be mapped to a credential.
    __tablename__: ClassVar[str] = "auth_lockout_buckets"
    __table_args__ = {"extend_existing": True}

    bucket_id: str = Field(primary_key=True)
    bucket_type: str = Field(index=True)
    source_context: str = Field(index=True)
    credential_id: str = Field(index=True)
    attempts: int = Field(default=0)
    window_started_at: float = Field(default=0)
    locked_until: float | None = Field(default=None, index=True)
    updated_at: float = Field(default_factory=time.time, index=True)


class AuthLockoutReservation(SQLModel, table=True):
    """One independently expiring, exactly-once authentication verification lease."""

    __tablename__: ClassVar[str] = "auth_lockout_reservations"
    __table_args__ = {"extend_existing": True}

    token: str = Field(primary_key=True)
    credential_bucket_id: str = Field(index=True)
    source_bucket_id: str = Field(index=True)
    expires_at: float = Field(index=True)
    deadline_at: float = Field(index=True)
    created_at: float = Field(default_factory=time.time, index=True)


class AuthLockoutMetric(SQLModel, table=True):
    """Durable singleton containing aggregate, label-free lockout counters."""

    __tablename__: ClassVar[str] = "auth_lockout_metrics"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_auth_lockout_metrics_singleton"),
        {"extend_existing": True},
    )

    id: int = Field(default=1, primary_key=True)
    failures_total: int = Field(default=0)
    lockouts_total: int = Field(default=0)
    source_lockouts_total: int = Field(default=0)
    recoveries_total: int = Field(default=0)
    capacity_rejections_total: int = Field(default=0)
    orphan_compactions_total: int = Field(default=0)
    updated_at: float = Field(default_factory=time.time)


class AuthLockoutIdentityKeyBinding(SQLModel, table=True):
    """Non-secret singleton binding lockout rows to one HMAC-key generation."""

    __tablename__: ClassVar[str] = "auth_lockout_identity_key_binding"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_auth_lockout_identity_key_binding_singleton"),
        {"extend_existing": True},
    )

    id: int = Field(default=1, primary_key=True)
    fingerprint: str = Field()
    generation: int = Field(default=1)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class AuthLockoutSchemaError(RuntimeError):
    """The durable authentication lockout schema is incompatible."""


class ApiKeyBootstrapConflict(RuntimeError):
    """A first-key bootstrap lost the durable database claim."""


class ApiKeyActiveLimitError(RuntimeError):
    """Creating or reactivating a key would exceed the configured active cap."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"active API key limit reached: {limit}")


class ApiKeyBootstrapClaim(SQLModel, table=True):
    """Permanent singleton proving that public key bootstrap was consumed."""

    __tablename__: ClassVar[str] = "api_key_bootstrap_claim"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_api_key_bootstrap_claim_singleton"),
        {"extend_existing": True},
    )

    id: int = Field(default=1, primary_key=True)
    initialized_at: float = Field(default_factory=time.time)


class ApiKey(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    label: str = Field(default="")
    key_hash: str = Field()
    is_active: bool = Field(default=True)
    created_at: float = Field(default_factory=time.time)

    @staticmethod
    async def has_any_key_async(session: AsyncSession) -> bool:
        """Return whether at least one active key can authenticate."""

        result = await session.exec(select(ApiKey).where(ApiKey.is_active == True))  # noqa: E712
        return result.first() is not None

    @staticmethod
    def has_any_key(session: Session) -> bool:
        """Return whether at least one active key can authenticate."""

        return session.exec(select(ApiKey).where(ApiKey.is_active == True)).first() is not None  # noqa: E712

    @staticmethod
    async def has_any_record_async(session: AsyncSession) -> bool:
        """Return whether any key row exists, including inactive history."""

        result = await session.exec(select(ApiKey.id).limit(1))
        return result.first() is not None

    @staticmethod
    async def bootstrap_is_claimed_async(session: AsyncSession) -> bool:
        """Return whether this database was ever initialized with an API key."""

        if await session.get(ApiKeyBootstrapClaim, 1) is not None:
            return True
        return await ApiKey.has_any_record_async(session)

    @staticmethod
    def store_key(session: Session, raw_key: str, label: str = "") -> "ApiKey":
        key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()
        obj = ApiKey(label=label, key_hash=key_hash)
        session.add(obj)
        session.commit()
        session.refresh(obj)
        return obj

    @staticmethod
    async def store_key_async(
        session: AsyncSession,
        raw_key: str,
        label: str = "",
        *,
        max_active_keys: int | None = None,
    ) -> "ApiKey":
        key_hash = (
            await asyncio.to_thread(bcrypt.hashpw, raw_key.encode(), bcrypt.gensalt(rounds=12))
        ).decode()
        if max_active_keys is not None:
            await session.exec(text("BEGIN IMMEDIATE"))
            active_count = await session.exec(
                select(func.count(ApiKey.id)).where(ApiKey.is_active == True)  # noqa: E712
            )
            if int(active_count.one()) >= max_active_keys:
                await session.rollback()
                raise ApiKeyActiveLimitError(max_active_keys)
        obj = ApiKey(label=label, key_hash=key_hash)
        session.add(obj)
        await session.commit()
        await session.refresh(obj)
        return obj

    @staticmethod
    async def bootstrap_first_key_async(
        session: AsyncSession,
        raw_key: str,
        label: str = "",
    ) -> "ApiKey":
        """Atomically consume the permanent claim and store the first key.

        The singleton primary key is the cross-process correctness boundary.
        The claim and bcrypt hash share one transaction, so a failed insert
        cannot permanently consume bootstrap without also storing the key.
        """

        key_hash = (
            await asyncio.to_thread(
                bcrypt.hashpw,
                raw_key.encode(),
                bcrypt.gensalt(rounds=12),
            )
        ).decode()
        claim = ApiKeyBootstrapClaim(id=1)
        obj = ApiKey(label=label, key_hash=key_hash)
        session.add(claim)
        session.add(obj)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise ApiKeyBootstrapConflict from None
        await session.refresh(obj)
        return obj

    @staticmethod
    def verify_any(
        session: Session,
        provided_key: str,
        *,
        max_active_keys: int,
    ) -> bool:
        rows = list(
            session.exec(
                select(ApiKey)
                .where(ApiKey.is_active == True)  # noqa: E712
                .order_by(ApiKey.id)
                .limit(max_active_keys)
            ).all()
        )
        for row in rows:
            try:
                if bcrypt.checkpw(provided_key.encode(), row.key_hash.encode()):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    async def verify_any_async(
        session: AsyncSession,
        provided_key: str,
        *,
        max_active_keys: int,
    ) -> bool:
        result = await session.exec(
            select(ApiKey)
            .where(ApiKey.is_active == True)  # noqa: E712
            .order_by(ApiKey.id)
            .limit(max_active_keys)
        )
        rows = list(result.all())
        provided = provided_key.encode()
        for row in rows:
            try:
                if await asyncio.to_thread(bcrypt.checkpw, provided, row.key_hash.encode()):
                    return True
            except Exception:
                continue
        return False


def _migration_table_columns(target_engine: Engine, table: str) -> set[str] | None:
    """Return migration columns and fail closed when schema inspection fails."""
    try:
        inspector = inspect(target_engine)
        if not inspector.has_table(table):
            return None
        return {str(column["name"]) for column in inspector.get_columns(table)}
    except Exception as error:  # noqa: BLE001
        raise DatabaseStartupError(
            f"Failed to inspect SQLite schema for required migration table {table}."
        ) from error


def _migrate_api_key_bootstrap_claim(target_engine: Engine | None = None) -> None:
    """Permanently close bootstrap for every legacy database with key history."""

    if target_engine is None:
        target_engine = get_engine()
    claim_table = ApiKeyBootstrapClaim.__tablename__
    key_table = ApiKey.__tablename__
    if _migration_table_columns(target_engine, claim_table) is None:
        return
    if _migration_table_columns(target_engine, key_table) is None:
        return
    with target_engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT OR IGNORE INTO {claim_table} (id, initialized_at) "
                f"SELECT 1, :initialized_at WHERE EXISTS (SELECT 1 FROM {key_table} LIMIT 1)"
            ),
            {"initialized_at": time.time()},
        )


def _validate_auth_table_schema(connection: Connection, model: type[SQLModel]) -> None:
    """Reject drift that would invalidate lockout upserts or singleton counters."""

    model_table = cast(Any, model).__table__
    table_name = model_table.name
    try:
        inspector = inspect(connection)
        actual_columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        expected_columns = {column.name: column for column in model_table.columns}
        if set(actual_columns) != set(expected_columns):
            raise AuthLockoutSchemaError(f"incompatible {table_name} columns")

        actual_pk = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())
        expected_pk = tuple(column.name for column in model_table.primary_key.columns)
        if actual_pk != expected_pk:
            raise AuthLockoutSchemaError(f"incompatible {table_name} primary key")

        for name, expected in expected_columns.items():
            actual = actual_columns[name]
            expected_affinity = expected.type._type_affinity.__name__
            actual_affinity = cast(Any, actual["type"])._type_affinity.__name__
            if expected_affinity != actual_affinity:
                raise AuthLockoutSchemaError(f"incompatible {table_name}.{name} type")
            if not expected.primary_key and bool(actual["nullable"]) != bool(expected.nullable):
                raise AuthLockoutSchemaError(f"incompatible {table_name}.{name} nullability")

        if model in (AuthLockoutMetric, AuthLockoutIdentityKeyBinding):
            checks = {
                "".join(str(item.get("sqltext", "")).lower().split())
                for item in inspector.get_check_constraints(table_name)
            }
            if "id=1" not in checks:
                raise AuthLockoutSchemaError(f"incompatible {table_name} singleton constraint")
    except AuthLockoutSchemaError:
        raise
    except Exception as error:  # noqa: BLE001
        raise DatabaseStartupError(
            f"Failed to inspect SQLite schema for required migration table {table_name}."
        ) from error


def _migrate_auth_lockout_reservation_deadline_unchecked(connection: Connection) -> None:
    """Add terminal deadlines only to the exact previous reservation schema."""

    model_table = cast(Any, AuthLockoutReservation).__table__
    table_name = model_table.name
    inspector = inspect(connection)
    actual_columns = {column["name"]: column for column in inspector.get_columns(table_name)}
    if "deadline_at" in actual_columns:
        return

    expected_columns = {
        column.name: column for column in model_table.columns if column.name != "deadline_at"
    }
    if set(actual_columns) != set(expected_columns):
        raise AuthLockoutSchemaError(f"incompatible {table_name} columns")
    actual_pk = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())
    expected_pk = tuple(column.name for column in model_table.primary_key.columns)
    if actual_pk != expected_pk:
        raise AuthLockoutSchemaError(f"incompatible {table_name} primary key")
    for name, expected in expected_columns.items():
        actual = actual_columns[name]
        expected_affinity = expected.type._type_affinity.__name__
        actual_affinity = cast(Any, actual["type"])._type_affinity.__name__
        if expected_affinity != actual_affinity:
            raise AuthLockoutSchemaError(f"incompatible {table_name}.{name} type")
        if not expected.primary_key and bool(actual["nullable"]) != bool(expected.nullable):
            raise AuthLockoutSchemaError(f"incompatible {table_name}.{name} nullability")

    from proxbox_api.services.auth_lockout import default_verification_max_seconds

    connection.exec_driver_sql(
        f"ALTER TABLE {table_name} ADD COLUMN deadline_at FLOAT NOT NULL DEFAULT 0"
    )
    connection.execute(
        text(
            f"UPDATE {table_name} SET deadline_at = created_at + :default_verification_max_seconds"
        ),
        {"default_verification_max_seconds": default_verification_max_seconds()},
    )


def _migrate_auth_lockout_reservation_deadline(connection: Connection) -> None:
    table_name = AuthLockoutReservation.__tablename__
    try:
        _migrate_auth_lockout_reservation_deadline_unchecked(connection)
    except AuthLockoutSchemaError:
        raise
    except Exception as error:  # noqa: BLE001
        raise DatabaseStartupError(
            f"Failed to inspect SQLite schema for required migration table {table_name}."
        ) from error


def validate_auth_lockout_schema(connection: Connection) -> None:
    """Validate every current lockout table exactly without creating or migrating it."""

    _validate_auth_table_schema(connection, AuthLockout)
    _validate_auth_table_schema(connection, AuthLockoutReservation)
    _validate_auth_table_schema(connection, AuthLockoutMetric)
    _validate_auth_table_schema(connection, AuthLockoutIdentityKeyBinding)


def _migrate_auth_lockout_schema(target_engine: Engine | None = None) -> None:
    """Create and validate versioned auth tables under one reserved transaction.

    The legacy ``authlockout`` table remains untouched as a rollback-compatible
    empty or populated IP-only table. Its rows are deliberately not imported,
    because doing so would recreate the cross-credential denial of service.
    """

    selected_engine = target_engine or get_engine()
    with selected_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        cast(Any, AuthLockout).__table__.create(connection, checkfirst=True)
        cast(Any, AuthLockoutReservation).__table__.create(connection, checkfirst=True)
        cast(Any, AuthLockoutMetric).__table__.create(connection, checkfirst=True)
        cast(Any, AuthLockoutIdentityKeyBinding).__table__.create(
            connection,
            checkfirst=True,
        )
        _migrate_auth_lockout_reservation_deadline(connection)
        validate_auth_lockout_schema(connection)
        binding = connection.execute(
            text(
                "SELECT fingerprint, generation FROM auth_lockout_identity_key_binding WHERE id = 1"
            )
        ).fetchone()
        if binding is None:
            state_exists = bool(
                connection.execute(
                    text(
                        "SELECT "
                        "EXISTS(SELECT 1 FROM auth_lockout_buckets LIMIT 1) OR "
                        "EXISTS(SELECT 1 FROM auth_lockout_reservations LIMIT 1)"
                    )
                ).scalar_one()
            )
            if state_exists:
                raise DatabaseConfigurationError(
                    "The authentication lockout identity-key binding is missing while "
                    "opaque lockout state still exists. Restore the binding and bound key "
                    "together, or perform the documented offline identity-key rebind "
                    "procedure before startup."
                )
        from proxbox_api.services.auth_lockout import initialize_auth_lockout_identity_key

        expected_fingerprint = str(binding.fingerprint) if binding is not None else None
        try:
            fingerprint = initialize_auth_lockout_identity_key(expected_fingerprint)
        except ValueError as error:
            raise DatabaseConfigurationError(
                "The authentication lockout identity key does not match the database "
                "binding. Restore the bound key or perform the documented offline "
                "identity-key rebind procedure before startup."
            ) from error
        if binding is None:
            timestamp = time.time()
            connection.execute(
                text(
                    "INSERT INTO auth_lockout_identity_key_binding "
                    "(id, fingerprint, generation, created_at, updated_at) "
                    "VALUES (1, :fingerprint, 1, :timestamp, :timestamp)"
                ),
                {"fingerprint": fingerprint, "timestamp": timestamp},
            )
        connection.commit()


def _migrate_proxmox_endpoint_columns() -> None:  # noqa: C901
    target_engine = get_engine()
    table = ProxmoxEndpoint.__tablename__
    existing = _migration_table_columns(target_engine, table)
    if existing is None:
        return
    stmts: list[str] = []
    if "timeout" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN timeout INTEGER")
    if "max_retries" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN max_retries INTEGER")
    if "retry_backoff" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN retry_backoff REAL")
    if "site_id" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN site_id INTEGER")
    if "site_slug" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN site_slug VARCHAR")
    if "site_name" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN site_name VARCHAR")
    if "tenant_id" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN tenant_id INTEGER")
    if "tenant_slug" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN tenant_slug VARCHAR")
    if "tenant_name" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN tenant_name VARCHAR")
    if "allow_writes" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN allow_writes BOOLEAN NOT NULL DEFAULT 0")
    if "allow_packer_template_builds" not in existing:
        stmts.append(
            f"ALTER TABLE {table} ADD COLUMN "
            "allow_packer_template_builds BOOLEAN NOT NULL DEFAULT 0"
        )
    if "access_methods" not in existing:
        # Backfill pre-existing rows to "api_ssh" (NON-BREAKING): SSH paths were
        # previously ungated, so defaulting legacy endpoints to "api" would 403
        # any in-use SSH terminal / cloud-image SSH on upgrade. New rows created
        # through the ORM use the model default "api" instead (see
        # ProxmoxEndpoint.access_methods).
        stmts.append(
            f"ALTER TABLE {table} ADD COLUMN access_methods VARCHAR NOT NULL DEFAULT 'api_ssh'"
        )
    if "enabled" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1")
    if "ssh_target_node" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN ssh_target_node VARCHAR")
    if "ssh_host" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN ssh_host VARCHAR")
    if "ssh_username" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN ssh_username VARCHAR")
    if "ssh_port" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN ssh_port INTEGER NOT NULL DEFAULT 22")
    if "ssh_identity_file" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN ssh_identity_file VARCHAR")
    if "ssh_known_host_fingerprint" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN ssh_known_host_fingerprint VARCHAR")
    if "verify_ssl" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN verify_ssl BOOLEAN NOT NULL DEFAULT 1")
    if not stmts:
        return
    with target_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _migrate_netbox_endpoint_columns() -> None:
    target_engine = get_engine()
    table = NetBoxEndpoint.__tablename__
    existing = _migration_table_columns(target_engine, table)
    if existing is None:
        return
    stmts: list[str] = []
    if "token_version" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN token_version VARCHAR DEFAULT 'v1'")
    if "token_key" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN token_key VARCHAR")
    if "enabled" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1")
    if "verify_ssl" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN verify_ssl BOOLEAN NOT NULL DEFAULT 1")
    if not stmts:
        return
    with target_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.execute(
            text(
                f"UPDATE {table} SET token_version = 'v1' "
                "WHERE token_version IS NULL OR TRIM(COALESCE(token_version, '')) = ''"
            )
        )
        # Ensure verify_ssl is never NULL (NULL → True means "verify by default").
        conn.execute(text(f"UPDATE {table} SET verify_ssl = 1 WHERE verify_ssl IS NULL"))


def _migrate_deletion_request_columns() -> None:
    target_engine = get_engine()
    table = DeletionRequestRecord.__tablename__
    existing = _migration_table_columns(target_engine, table)
    if existing is None:
        return
    stmts: list[str] = []
    column_specs = {
        "endpoint_id": "INTEGER NOT NULL DEFAULT 0",
        "vmid": "INTEGER NOT NULL DEFAULT 0",
        "node": "VARCHAR NOT NULL DEFAULT ''",
        "kind": "VARCHAR NOT NULL DEFAULT 'qemu'",
        "state": "VARCHAR NOT NULL DEFAULT 'pending'",
        "created_at": "REAL NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    }
    for column, spec in column_specs.items():
        if column not in existing:
            stmts.append(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
    if not stmts:
        return
    now = time.time()
    with target_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.execute(
            text(
                f"UPDATE {table} SET created_at = :now WHERE created_at IS NULL OR created_at = 0"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                f"UPDATE {table} SET updated_at = :now WHERE updated_at IS NULL OR updated_at = 0"
            ),
            {"now": now},
        )


def _migrate_pbs_endpoint_columns() -> None:
    target_engine = get_engine()
    table = PBSEndpoint.__tablename__
    existing = _migration_table_columns(target_engine, table)
    if existing is None:
        return
    # PBS commonly uses self-signed certs, so verify_ssl defaults to 0 (False).
    column_specs: dict[str, str] = {
        "fingerprint": "VARCHAR",
        "allow_writes": "BOOLEAN NOT NULL DEFAULT 0",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 30",
        "last_seen_at": "REAL",
        "verify_ssl": "BOOLEAN NOT NULL DEFAULT 0",
    }
    stmts = [
        f"ALTER TABLE {table} ADD COLUMN {col} {spec}"
        for col, spec in column_specs.items()
        if col not in existing
    ]
    if not stmts:
        return
    with target_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _migrate_pdm_endpoint_columns() -> None:
    target_engine = get_engine()
    table = PDMEndpoint.__tablename__
    existing = _migration_table_columns(target_engine, table)
    if existing is None:
        return
    column_specs: dict[str, str] = {
        "fingerprint": "VARCHAR",
        "allow_writes": "BOOLEAN NOT NULL DEFAULT 0",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 30",
        "last_seen_at": "REAL",
        "verify_ssl": "BOOLEAN NOT NULL DEFAULT 1",
    }
    stmts = [
        f"ALTER TABLE {table} ADD COLUMN {col} {spec}"
        for col, spec in column_specs.items()
        if col not in existing
    ]
    if not stmts:
        return
    with target_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _migrate_ceph_operation_run_columns() -> None:
    target_engine = get_engine()
    table = CephOperationRunRecord.__tablename__
    existing = _migration_table_columns(target_engine, table)
    if existing is None:
        return
    column_specs: dict[str, str] = {
        "plan_id": "VARCHAR",
        "status": "VARCHAR NOT NULL DEFAULT 'pending'",
        "actor": "VARCHAR",
        "source_branch_schema_id": "VARCHAR",
        "provider": "VARCHAR NOT NULL DEFAULT 'proxmox'",
        "request_summary": "JSON NOT NULL DEFAULT '{}'",
        "provider_task_refs": "JSON NOT NULL DEFAULT '[]'",
        "created_at": "REAL NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
        "warnings": "JSON NOT NULL DEFAULT '[]'",
        "errors": "JSON NOT NULL DEFAULT '[]'",
        "result_summary": "JSON NOT NULL DEFAULT '{}'",
    }
    stmts = [
        f"ALTER TABLE {table} ADD COLUMN {col} {spec}"
        for col, spec in column_specs.items()
        if col not in existing
    ]
    if not stmts:
        return
    now = time.time()
    with target_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.execute(
            text(f"UPDATE {table} SET created_at = :now WHERE created_at = 0"), {"now": now}
        )
        conn.execute(
            text(f"UPDATE {table} SET updated_at = :now WHERE updated_at = 0"), {"now": now}
        )


def _migrate_prometheus_source_columns() -> None:
    target_engine = get_engine()
    table = PrometheusSource.__tablename__
    existing = _migration_table_columns(target_engine, table)
    if existing is None:
        return
    column_specs: dict[str, str] = {
        "bearer_token": "VARCHAR",
        "credential_ref": "VARCHAR",
        "cluster_ref": "VARCHAR",
        "verify_ssl": "BOOLEAN NOT NULL DEFAULT 1",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 15",
        "scrape_interval_seconds": "INTEGER NOT NULL DEFAULT 60",
        "last_seen_at": "REAL",
    }
    stmts = [
        f"ALTER TABLE {table} ADD COLUMN {col} {spec}"
        for col, spec in column_specs.items()
        if col not in existing
    ]
    if not stmts:
        return
    with target_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _migrate_ceph_dashboard_endpoint_columns() -> None:
    target_engine = get_engine()
    table = CephDashboardEndpoint.__tablename__
    existing = _migration_table_columns(target_engine, table)
    if existing is None:
        return
    column_specs: dict[str, str] = {
        "username": "VARCHAR",
        "password": "VARCHAR",
        "token": "VARCHAR",
        "credential_ref": "VARCHAR",
        "cluster_ref": "VARCHAR",
        "api_version": "VARCHAR NOT NULL DEFAULT '1.0'",
        "verify_ssl": "BOOLEAN NOT NULL DEFAULT 1",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 30",
        "last_seen_at": "REAL",
    }
    stmts = [
        f"ALTER TABLE {table} ADD COLUMN {col} {spec}"
        for col, spec in column_specs.items()
        if col not in existing
    ]
    if not stmts:
        return
    with target_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _migrate_ceph_external_cluster_columns() -> None:
    target_engine = get_engine()
    table = CephExternalCluster.__tablename__
    existing = _migration_table_columns(target_engine, table)
    if existing is None:
        return
    column_specs: dict[str, str] = {
        "cluster_ref": "VARCHAR",
        "ceph_version_hint": "VARCHAR",
        "dashboard_endpoint_id": "INTEGER",
        "prometheus_source_id": "INTEGER",
        "rgw_admin_url": "VARCHAR",
        "rgw_access_key": "VARCHAR",
        "rgw_secret_key": "VARCHAR",
        "ssh_host": "VARCHAR",
        "ssh_user": "VARCHAR",
        "ssh_credential_ref": "VARCHAR",
        "verify_ssl": "BOOLEAN NOT NULL DEFAULT 1",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "last_seen_at": "REAL",
    }
    stmts = [
        f"ALTER TABLE {table} ADD COLUMN {col} {spec}"
        for col, spec in column_specs.items()
        if col not in existing
    ]
    if not stmts:
        return
    with target_engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _create_db_and_tables_unlocked() -> None:
    """Create tables and run migrations while the caller holds the startup lock."""
    target_engine = get_engine()
    _migrate_auth_lockout_schema(target_engine)
    SQLModel.metadata.create_all(target_engine)
    _migrate_api_key_bootstrap_claim(target_engine)
    _migrate_proxmox_endpoint_columns()
    _migrate_netbox_endpoint_columns()
    _migrate_deletion_request_columns()
    _migrate_pbs_endpoint_columns()
    _migrate_pdm_endpoint_columns()
    _migrate_ceph_operation_run_columns()
    _migrate_prometheus_source_columns()
    _migrate_ceph_dashboard_endpoint_columns()
    _migrate_ceph_external_cluster_columns()


def _warn_if_active_api_key_limit_exceeded(
    target_engine: Engine,
    environ: Mapping[str, str] | None,
) -> None:
    """Keep recovery authentication available while surfacing an unsafe legacy count."""

    from proxbox_api.services.auth_lockout import AuthLockoutPolicy

    limit = AuthLockoutPolicy.from_env(environ).max_active_keys
    with Session(target_engine) as session:
        result = session.exec(
            select(func.count(ApiKey.id)).where(ApiKey.is_active == True)  # noqa: E712
        )
        active_count = int(result.one())
    if active_count <= limit:
        return
    logger.error(
        "Active API key count %s exceeds PROXBOX_AUTH_MAX_ACTIVE_KEYS=%s. "
        "Authentication remains available through a bounded scan of the oldest %s "
        "active keys. Authenticate with one of those keys and call "
        "POST /auth/keys/{id}/deactivate until the count is within the cap. If none "
        "of the bounded keys is available, temporarily raise PROXBOX_AUTH_MAX_ACTIVE_KEYS, "
        "restart, deactivate excess keys, then restore the intended cap.",
        active_count,
        limit,
        limit,
        extra={
            "active_api_key_count": active_count,
            "active_api_key_limit": limit,
        },
    )


def initialize_database_and_schema(
    environ: Mapping[str, str] | None = None,
) -> SQLiteDatabaseTarget:
    """Verify one target and serialize all startup schema work across processes."""
    target = resolve_database_target(environ)
    with _database_runtime_lock, _database_startup_advisory_lock(target):
        _consume_fresh_database_override(target)
        _audit_fresh_database_override(target)
        initialized_target = _initialize_database_target(target)
        _create_db_and_tables_unlocked()
        _warn_if_active_api_key_limit_exceeded(get_engine(), environ)
        _acquire_runtime_database_lease(target)
        return initialized_target


def create_db_and_tables() -> None:
    """Re-run idempotent schema setup under the target-specific startup lock."""
    target = database_target
    if target is None:
        raise DatabaseNotInitializedError(
            "Database schema cannot be initialized before application lifespan startup."
        )
    with _database_runtime_lock, _database_startup_advisory_lock(target):
        _create_db_and_tables_unlocked()


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_async_sessionmaker()() as session:
        yield session


DatabaseSessionDep = Annotated[Session, Depends(get_session)]
AsyncDatabaseSessionDep = Annotated[AsyncSession, Depends(get_async_session)]
