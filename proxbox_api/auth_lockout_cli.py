"""Local administrative inspection and recovery for authentication lockouts."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence, cast

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, create_engine

from proxbox_api.database import AuthLockout, AuthLockoutMetric, configure_sqlite_engine
from proxbox_api.services.auth_lockout import (
    AuthLockoutService,
    LockoutSelectionError,
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
    models = (AuthLockout, AuthLockoutMetric)
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


def main(argv: Sequence[str] | None = None, *, target_engine: Engine | None = None) -> int:
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
        _require_lockout_schema(selected_engine)
        with Session(selected_engine) as session:
            if args.command == "list":
                return _list(session)
            if args.all:
                cleared = AuthLockoutService.clear_all(session)
            else:
                cleared = AuthLockoutService.clear_by_safe_id(session, args.safe_id)
    except (LockoutDatabaseError, LockoutSelectionError) as exc:
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
