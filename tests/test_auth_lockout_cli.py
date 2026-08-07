"""Local auth lockout recovery CLI tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

from sqlmodel import Session, create_engine

from proxbox_api import auth
from proxbox_api.auth_lockout_cli import main
from proxbox_api.database import ApiKey, AuthLockout
from proxbox_api.services.auth_lockout import (
    AuthLockoutPolicy,
    AuthLockoutService,
    build_lockout_identity,
    resolve_auth_source_context,
)

RAW_STALE_KEY = "stale-cli-key-that-must-never-be-rendered"


def test_cli_inspects_and_clears_bucket_while_http_identity_is_locked(
    db_engine,
    capsys,
) -> None:
    policy = AuthLockoutPolicy(threshold=1, window_seconds=300)
    identity = build_lockout_identity(
        resolve_auth_source_context("127.0.0.1", None, ()),
        RAW_STALE_KEY,
    )
    with Session(db_engine) as session:
        ApiKey.store_key(
            session,
            "valid-cli-recovery-key-aaaaaaaaaaaaaaaa",
            label="cli-recovery",
        )
        session.rollback()
        auth.check_auth_header_with_session(
            session,
            RAW_STALE_KEY,
            "127.0.0.1",
            policy,
        )
        assert AuthLockoutService.is_locked(session, identity)
        authorized, message = auth.check_auth_header_with_session(
            session,
            RAW_STALE_KEY,
            "127.0.0.1",
            policy,
        )
        assert authorized is False
        assert message is not None and "Too many failed" in message

    assert main(["list"], target_engine=db_engine) == 0
    listing = capsys.readouterr().out
    assert identity.safe_id in listing
    assert identity.credential_id in listing
    assert RAW_STALE_KEY not in listing
    assert identity.bucket_id not in listing

    assert main(["clear", "--id", identity.safe_id], target_engine=db_engine) == 0
    assert "Cleared 1" in capsys.readouterr().out
    with Session(db_engine) as session:
        assert not AuthLockoutService.is_locked(session, identity)
        authorized, message = auth.check_auth_header_with_session(
            session,
            RAW_STALE_KEY,
            "127.0.0.1",
            policy,
        )
        assert authorized is False
        assert message is not None and "Invalid API key" in message


def test_cli_rejects_unsafe_short_selector(db_engine, capsys) -> None:
    assert main(["clear", "--id", "abc"], target_engine=db_engine) == 2
    assert "at least 8 hexadecimal" in capsys.readouterr().err


def test_cli_requires_explicit_database_and_never_creates_missing_path(
    tmp_path,
    capsys,
) -> None:
    missing = tmp_path / "missing.db"
    assert main(["list"]) == 2
    assert "--database is required" in capsys.readouterr().err

    assert main(["--database", str(missing), "list"]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert not missing.exists()


def test_cli_rejects_existing_database_without_lockout_schema(tmp_path, capsys) -> None:
    database_path = tmp_path / "empty.db"
    target = create_engine(f"sqlite:///{database_path}")
    try:
        with target.begin():
            pass
    finally:
        target.dispose()

    assert main(["--database", str(database_path), "list"]) == 2
    assert "no auth_lockout_buckets table" in capsys.readouterr().err


def test_cli_rejects_partial_lockout_schema(tmp_path, capsys) -> None:
    database_path = tmp_path / "partial.db"
    target = create_engine(f"sqlite:///{database_path}")
    try:
        cast(Any, AuthLockout).__table__.create(target)
    finally:
        target.dispose()

    assert main(["--database", str(database_path), "list"]) == 2
    assert "no auth_lockout_metrics table" in capsys.readouterr().err


def test_cli_list_opens_explicit_database_read_only(db_engine, capsys) -> None:
    database_path = Path(db_engine.url.database)
    db_engine.dispose()
    before = hashlib.sha256(database_path.read_bytes()).digest()

    assert main(["--database", str(database_path), "list"]) == 0
    assert "No authentication lockout buckets" in capsys.readouterr().out
    assert hashlib.sha256(database_path.read_bytes()).digest() == before
