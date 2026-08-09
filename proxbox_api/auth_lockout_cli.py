"""Local administrative inspection and recovery for authentication lockouts."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence, cast

from sqlalchemy import delete, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, create_engine

from proxbox_api.database import (
    AuthLockout,
    AuthLockoutIdentityKeyBinding,
    AuthLockoutMetric,
    AuthLockoutReservation,
    DatabaseStartupError,
    configure_sqlite_engine,
    offline_database_maintenance_lock,
)
from proxbox_api.services.auth_lockout import (
    AuthLockoutService,
    LockoutConfigurationError,
    LockoutSelectionError,
    auth_lockout_identity_key_fingerprint_from_material,
)


class LockoutDatabaseError(RuntimeError):
    """The explicitly selected database is absent or lacks the lockout schema."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proxbox-auth-lockout",
        description="Inspect or clear local auth lockout buckets without using HTTP.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Existing proxbox-api SQLite database (required outside tests)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List secret-free lockout bucket diagnostics")
    clear = commands.add_parser("clear", help="Clear one bucket or all buckets")
    selector = clear.add_mutually_exclusive_group(required=True)
    selector.add_argument("--id", dest="safe_id", help="Safe bucket ID shown by list")
    selector.add_argument("--all", action="store_true", help="Clear every lockout bucket")
    rebind = commands.add_parser(
        "rebind-key",
        help="Offline reset and rebind after identity-key loss or planned rotation",
    )
    rebind.add_argument(
        "--key-file",
        type=Path,
        required=True,
        help="Existing private file containing the replacement identity key",
    )
    rebind.add_argument(
        "--confirm-reset-lockouts",
        action="store_true",
        required=True,
        help="Acknowledge that every durable lockout bucket will be cleared",
    )
    return parser


def _format_timestamp(value: float | None) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")


def _list(session: Session) -> int:
    rows = AuthLockoutService.list_rows(session)
    if not rows:
        print("No authentication lockout buckets.")
        return 0

    print(
        "ID           TYPE        SOURCE_CONTEXT                         CREDENTIAL   ATTEMPTS  STATUS"
    )
    now = time.time()
    for row in rows:
        status = (
            f"locked-until={_format_timestamp(row.locked_until)}"
            if row.locked_until is not None and now < row.locked_until
            else "observed"
        )
        print(
            f"{row.bucket_id[:12]:<12} {row.bucket_type:<11} {row.source_context:<38} "
            f"{(row.credential_id or '-'):<12} {row.attempts:<9} {status}"
        )
    return 0


def _database_engine(path: Path, *, read_only: bool) -> Engine:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise LockoutDatabaseError(f"database does not exist: {resolved}")
    if read_only:
        target = create_engine(
            f"sqlite:///file:{resolved.as_posix()}?mode=ro&uri=true",
            connect_args={"check_same_thread": False},
        )
    else:
        target = create_engine(
            f"sqlite:///{resolved}",
            connect_args={"check_same_thread": False},
        )
        configure_sqlite_engine(target)
    return target


def _require_lockout_schema(target_engine: Engine) -> None:
    inspector = inspect(target_engine)
    models = (
        AuthLockout,
        AuthLockoutReservation,
        AuthLockoutMetric,
        AuthLockoutIdentityKeyBinding,
    )
    for model in models:
        table = model.__tablename__
        if not inspector.has_table(table):
            raise LockoutDatabaseError(f"database has no {table} table")
        expected = {column.name for column in cast(Any, model).__table__.columns}
        actual = {column["name"] for column in inspector.get_columns(table)}
        missing = sorted(expected - actual)
        if missing:
            raise LockoutDatabaseError(
                f"database has an incompatible {table} schema; missing: {', '.join(missing)}"
            )


def _replacement_key_fingerprint(path: Path) -> str:
    """Read one private regular key file without following a symlink."""

    candidate = path.expanduser()
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(candidate, flags)
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise LockoutDatabaseError(f"identity key file is unavailable: {candidate}") from error
    assert descriptor is not None
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise LockoutDatabaseError("identity key file must be a regular, non-symlink file")
    if metadata.st_mode & 0o077:
        os.close(descriptor)
        raise LockoutDatabaseError("identity key file must not be accessible by group or others")
    try:
        material_bytes = os.read(descriptor, 4097)
        if len(material_bytes) > 4096:
            raise LockoutDatabaseError("identity key file must not exceed 4096 bytes")
        material = material_bytes.decode("utf-8").strip()
        return auth_lockout_identity_key_fingerprint_from_material(material)
    except (OSError, UnicodeError, LockoutConfigurationError) as error:
        raise LockoutDatabaseError("identity key file is invalid or unreadable") from error
    finally:
        os.close(descriptor)


def _rebind_identity_key(session: Session, fingerprint: str) -> tuple[int, int]:
    """Atomically clear incompatible buckets and advance the bound generation."""

    timestamp = time.time()
    binding = session.get(AuthLockoutIdentityKeyBinding, 1)
    session.exec(cast(Any, delete(AuthLockoutReservation)))
    result = session.exec(cast(Any, delete(AuthLockout)))
    cleared = int(cast(Any, result).rowcount or 0)
    if binding is None:
        binding = AuthLockoutIdentityKeyBinding(
            id=1,
            fingerprint=fingerprint,
            generation=1,
            created_at=timestamp,
            updated_at=timestamp,
        )
    else:
        binding.fingerprint = fingerprint
        binding.generation += 1
        binding.updated_at = timestamp
    session.add(binding)
    counters = session.get(AuthLockoutMetric, 1)
    if counters is not None:
        counters.recoveries_total += cleared
        counters.updated_at = timestamp
        session.add(counters)
    session.commit()
    return cleared, binding.generation


def main(  # noqa: C901
    argv: Sequence[str] | None = None,
    *,
    target_engine: Engine | None = None,
) -> int:
    """Run the local CLI; ``target_engine`` is an in-process test seam."""

    args = _build_parser().parse_args(argv)
    owns_engine = target_engine is None
    if target_engine is None:
        if args.database is None:
            print("error: --database is required", file=sys.stderr)
            return 2
        try:
            selected_engine = _database_engine(
                args.database,
                read_only=args.command == "list",
            )
        except LockoutDatabaseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        selected_engine = target_engine

    try:
        if args.command == "rebind-key":
            if args.database is None:
                raise LockoutDatabaseError("--database is required")
            fingerprint = _replacement_key_fingerprint(args.key_file)
            with offline_database_maintenance_lock(args.database):
                _require_lockout_schema(selected_engine)
                with Session(selected_engine) as session:
                    cleared, generation = _rebind_identity_key(session, fingerprint)
            print(
                "Rebound authentication identity key generation "
                f"{generation}; cleared {cleared} lockout bucket(s)."
            )
            return 0
        _require_lockout_schema(selected_engine)
        with Session(selected_engine) as session:
            if args.command == "list":
                return _list(session)
            if args.all:
                cleared = AuthLockoutService.clear_all(session)
            else:
                cleared = AuthLockoutService.clear_by_safe_id(session, args.safe_id)
    except (DatabaseStartupError, LockoutDatabaseError, LockoutSelectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print("error: unable to access the lockout database", file=sys.stderr)
        return 2
    finally:
        if owns_engine:
            selected_engine.dispose()

    print(f"Cleared {cleared} authentication lockout bucket(s).")
    return 0 if cleared else 1


if __name__ == "__main__":
    raise SystemExit(main())
