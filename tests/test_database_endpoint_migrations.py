"""Regression tests for local SQLite endpoint table repair helpers."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlmodel import create_engine

from proxbox_api import database


def _make_legacy_endpoint_table(engine, table: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE {table} (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    host VARCHAR NOT NULL,
                    port INTEGER NOT NULL,
                    token_id VARCHAR NOT NULL,
                    token_secret VARCHAR NOT NULL,
                    verify_ssl BOOLEAN NOT NULL DEFAULT 1
                )
                """
            )
        )


def _columns(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def test_pbs_endpoint_migration_adds_enabled_to_legacy_table(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'pbs.db'}")
    monkeypatch.setattr(database, "engine", engine)
    table = database.PBSEndpoint.__tablename__
    _make_legacy_endpoint_table(engine, table)

    database._migrate_pbs_endpoint_columns()

    assert {
        "fingerprint",
        "allow_writes",
        "enabled",
        "timeout_seconds",
        "last_seen_at",
    } <= _columns(engine, table)
    engine.dispose()


def test_pdm_endpoint_migration_adds_enabled_to_legacy_table(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'pdm.db'}")
    monkeypatch.setattr(database, "engine", engine)
    table = database.PDMEndpoint.__tablename__
    _make_legacy_endpoint_table(engine, table)

    database._migrate_pdm_endpoint_columns()

    assert {
        "fingerprint",
        "allow_writes",
        "enabled",
        "timeout_seconds",
        "last_seen_at",
    } <= _columns(engine, table)
    engine.dispose()
