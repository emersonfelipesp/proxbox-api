"""Regression tests for local SQLite endpoint table repair helpers."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlmodel import create_engine

from proxbox_api import database


def _make_startup_lock_target(database_path: str) -> database.SQLiteDatabaseTarget:
    return database.SQLiteDatabaseTarget(
        path=Path(database_path),
        source=database.DatabaseConfigurationSource.PROXBOX_DATABASE_PATH,
    )


def _hold_database_startup_lock(
    database_path: str,
    acquired: Any,
    release: Any,
) -> None:
    with database._database_startup_advisory_lock(_make_startup_lock_target(database_path)):
        acquired.set()
        release.wait(timeout=5)


def _stop_process(process: Any) -> None:
    process.join(timeout=2)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)


def test_database_startup_lock_serializes_worker_processes(tmp_path):
    context = multiprocessing.get_context("fork")
    database_path = str(tmp_path / "database.db")
    first_acquired = context.Event()
    first_release = context.Event()
    second_acquired = context.Event()
    second_release = context.Event()
    first = context.Process(
        target=_hold_database_startup_lock,
        args=(database_path, first_acquired, first_release),
    )
    second = context.Process(
        target=_hold_database_startup_lock,
        args=(database_path, second_acquired, second_release),
    )

    try:
        first.start()
        assert first_acquired.wait(timeout=2)

        second.start()
        assert not second_acquired.wait(timeout=0.25)

        first_release.set()
        assert second_acquired.wait(timeout=2)
    finally:
        first_release.set()
        second_release.set()
        _stop_process(first)
        _stop_process(second)

    assert first.exitcode == 0
    assert second.exitcode == 0


def test_database_startup_lock_releases_after_exception(tmp_path):
    database_path = str(tmp_path / "database.db")
    with pytest.raises(RuntimeError, match="startup failed"):
        with database._database_startup_advisory_lock(_make_startup_lock_target(database_path)):
            raise RuntimeError("startup failed")

    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_database_startup_lock,
        args=(database_path, acquired, release),
    )
    try:
        process.start()
        assert acquired.wait(timeout=2)
    finally:
        release.set()
        _stop_process(process)

    assert process.exitcode == 0


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
        "allow_packer_template_builds",
        "ssh_target_node",
        "ssh_host",
        "ssh_username",
        "ssh_port",
        "ssh_identity_file",
        "ssh_known_host_fingerprint",
    } <= _columns(engine, table)
    with engine.begin() as conn:
        value, allow_packer_template_builds, ssh_port = conn.execute(
            text(f"SELECT access_methods, allow_packer_template_builds, ssh_port FROM {table}")
        ).one()
    # NON-BREAKING backfill: pre-existing rows keep the SSH transport.
    assert value == "api_ssh"
    assert allow_packer_template_builds == 0
    column = next(
        item
        for item in inspect(engine).get_columns(table)
        if item["name"] == "allow_packer_template_builds"
    )
    assert column["nullable"] is False
    assert str(column["default"]).strip("()'\"") == "0"
    assert ssh_port == 22
    engine.dispose()
