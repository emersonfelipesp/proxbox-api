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


def _make_legacy_proxmox_endpoint_table(engine, table: str) -> None:
    """A pre-access_methods proxmoxendpoint table with one existing row."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE {table} (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    ip_address VARCHAR NOT NULL,
                    domain VARCHAR,
                    port INTEGER NOT NULL DEFAULT 8006,
                    username VARCHAR NOT NULL,
                    password VARCHAR,
                    verify_ssl BOOLEAN NOT NULL DEFAULT 1,
                    allow_writes BOOLEAN NOT NULL DEFAULT 0,
                    enabled BOOLEAN NOT NULL DEFAULT 1
                )
                """
            )
        )
        conn.execute(
            text(
                f"INSERT INTO {table} (name, ip_address, username) "
                "VALUES ('legacy', '10.0.0.9', 'root@pam')"
            )
        )


def test_proxmox_endpoint_migration_backfills_existing_rows_to_api_ssh(tmp_path, monkeypatch):
    """Existing endpoints must keep SSH working on upgrade (backfill ``api_ssh``)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'proxmox.db'}")
    monkeypatch.setattr(database, "engine", engine)
    table = database.ProxmoxEndpoint.__tablename__
    _make_legacy_proxmox_endpoint_table(engine, table)

    assert "access_methods" not in _columns(engine, table)

    database._migrate_proxmox_endpoint_columns()

    assert {
        "access_methods",
        "ssh_target_node",
        "ssh_host",
        "ssh_username",
        "ssh_port",
        "ssh_identity_file",
        "ssh_known_host_fingerprint",
    } <= _columns(engine, table)
    with engine.begin() as conn:
        value, ssh_port = conn.execute(text(f"SELECT access_methods, ssh_port FROM {table}")).one()
    # NON-BREAKING backfill: pre-existing rows keep the SSH transport.
    assert value == "api_ssh"
    assert ssh_port == 22
    engine.dispose()
