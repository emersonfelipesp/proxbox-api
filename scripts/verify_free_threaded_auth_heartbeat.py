"""Run the authentication heartbeat regression on an isolated free-threaded runtime."""

from __future__ import annotations

import os
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory

from pytest import MonkeyPatch
from sqlmodel import Session, SQLModel, create_engine

from proxbox_api import database as database_module
from proxbox_api.database import ApiKey
from proxbox_api.services.auth_lockout import (
    clear_runtime_auth_lockout_identity_key,
    initialize_auth_lockout_identity_key,
)

VALID_KEY = "valid-test-api-key-aaaaaaaaaaaaaaaaaaaaaaaa"


def main() -> None:
    """Create the focused fixture and execute the shared heartbeat assertion."""
    os.environ.setdefault("PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS", "1")
    os.environ.setdefault(
        "PROXBOX_AUTH_LOCKOUT_HMAC_KEY",
        "unit-test-only-auth-lockout-hmac-key-0000000000000000",
    )
    test_namespace = runpy.run_path("tests/test_auth_lockout.py")
    assertion = test_namespace["_assert_live_verifier_heartbeat_holds_global_slot_beyond_lease"]

    with TemporaryDirectory() as temporary_directory:
        engine = create_engine(
            f"sqlite:///{Path(temporary_directory) / 'test.db'}",
            connect_args={"check_same_thread": False},
        )
        database_module.configure_sqlite_engine(engine)
        SQLModel.metadata.create_all(engine)
        initialize_auth_lockout_identity_key(None)
        try:
            with Session(engine) as session:
                ApiKey.store_key(session, VALID_KEY, label="free-threaded-heartbeat")
                session.rollback()
            with MonkeyPatch.context() as monkeypatch:
                assertion(engine, VALID_KEY, monkeypatch)
        finally:
            engine.dispose()
            clear_runtime_auth_lockout_identity_key()


if __name__ == "__main__":
    main()
