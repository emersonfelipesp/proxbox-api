"""Regression tests for local SQLite endpoint table repair helpers."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

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

    assert "access_methods" in _columns(engine, table)
    with engine.begin() as conn:
        value = conn.execute(text(f"SELECT access_methods FROM {table}")).scalar_one()
    # NON-BREAKING backfill: pre-existing rows keep the SSH transport.
    assert value == "api_ssh"
    engine.dispose()


def test_ceph_control_plane_tables_and_audit_columns_upgrade_additively(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ceph-control.db'}")
    monkeypatch.setattr(database, "engine", engine)
    run_table = database.CephOperationRunRecord.__tablename__
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE {run_table} (id VARCHAR PRIMARY KEY)"))

    database._migrate_ceph_operation_run_columns()
    assert {
        "endpoint_id",
        "endpoint_config_revision",
        "plan_digest",
        "requester",
        "approver",
        "approval_id",
        "lease_expires_at",
        "lease_owner",
    } <= _columns(engine, run_table)

    SQLModel.metadata.create_all(engine)
    inspector = inspect(engine)
    assert inspector.has_table(database.CephPlanRecord.__tablename__)
    assert inspector.has_table(database.CephApprovalRecord.__tablename__)
    assert inspector.has_table(database.CephOperationEventRecord.__tablename__)
    assert inspector.has_table(database.CephProviderTaskClaimRecord.__tablename__)
    assert "token_hash" in _columns(engine, database.CephApprovalRecord.__tablename__)
    assert "endpoint_config_revision" in _columns(engine, database.CephPlanRecord.__tablename__)
    assert "endpoint_config_revision" in _columns(engine, database.CephApprovalRecord.__tablename__)
    approval_indexes = inspector.get_indexes(database.CephApprovalRecord.__tablename__)
    assert any(
        index["unique"] and index["column_names"] == ["plan_id"] for index in approval_indexes
    )
    assert any(
        index["unique"] and index["column_names"] == ["token_hash"] for index in approval_indexes
    )
    event_uniques = inspector.get_unique_constraints(
        database.CephOperationEventRecord.__tablename__
    )
    assert any(constraint["column_names"] == ["run_id", "sequence"] for constraint in event_uniques)
    claim_uniques = inspector.get_unique_constraints(
        database.CephProviderTaskClaimRecord.__tablename__
    )
    assert any(
        constraint["column_names"] == ["provider", "provider_task_ref"]
        for constraint in claim_uniques
    )
    engine.dispose()


def test_ceph_authority_migration_adds_revision_to_legacy_tables(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ceph-authority.db'}")
    monkeypatch.setattr(database, "engine", engine)
    plan_table = database.CephPlanRecord.__tablename__
    approval_table = database.CephApprovalRecord.__tablename__
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE {plan_table} (id VARCHAR PRIMARY KEY)"))
        conn.execute(text(f"CREATE TABLE {approval_table} (id VARCHAR PRIMARY KEY)"))

    database._migrate_ceph_authority_columns()
    database._migrate_ceph_authority_columns()

    assert "endpoint_config_revision" in _columns(engine, plan_table)
    assert "endpoint_config_revision" in _columns(engine, approval_table)
    engine.dispose()


def test_task_claim_migration_backfills_legacy_events_idempotently(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ceph-task-claims.db'}")
    monkeypatch.setattr(database, "engine", engine)
    database.CephOperationRunRecord.__table__.create(engine)
    database.CephOperationEventRecord.__table__.create(engine)
    task_ref = "UPID:node1:00000001:00000002:00000003:cephcreate:rbd:root@pam:"
    with Session(engine) as session:
        session.add(
            database.CephOperationRunRecord(
                id="legacy-task-run",
                endpoint_id=17,
                endpoint_config_revision="a" * 64,
                provider="proxmox",
                status="completed",
            )
        )
        for sequence, event_name in enumerate(
            ("provider_task_submitted", "provider_task_completed")
        ):
            session.add(
                database.CephOperationEventRecord(
                    run_id="legacy-task-run",
                    sequence=sequence,
                    operation_index=3,
                    operation_id="legacy-operation",
                    event=event_name,
                    status="completed",
                    code=event_name,
                    message="Legacy task evidence.",
                    provider_task_ref=task_ref,
                )
            )
        session.commit()

    database._migrate_ceph_provider_task_claim_table()
    database._migrate_ceph_provider_task_claim_table()

    with Session(engine) as session:
        claims = list(session.exec(select(database.CephProviderTaskClaimRecord)).all())
    assert len(claims) == 1
    assert claims[0].provider == "proxmox"
    assert claims[0].endpoint_id == 17
    assert claims[0].provider_task_ref == task_ref
    assert claims[0].run_id == "legacy-task-run"
    assert claims[0].operation_index == 3
    engine.dispose()


def _create_endpoint_scoped_task_claim_table(engine, table: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                CREATE TABLE {table} (
                    id INTEGER NOT NULL PRIMARY KEY,
                    provider VARCHAR NOT NULL,
                    endpoint_id INTEGER NOT NULL,
                    endpoint_config_revision VARCHAR,
                    provider_task_ref VARCHAR NOT NULL,
                    run_id VARCHAR NOT NULL,
                    operation_index INTEGER NOT NULL,
                    operation_id VARCHAR,
                    claimed_at FLOAT NOT NULL,
                    CONSTRAINT uq_ceph_provider_task_claim_provider_endpoint_ref
                        UNIQUE (provider, endpoint_id, provider_task_ref)
                )
                """
            )
        )


def test_task_claim_migration_rebuilds_endpoint_scoped_unique_key(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ceph-task-claim-global.db'}")
    monkeypatch.setattr(database, "engine", engine)
    table = database.CephProviderTaskClaimRecord.__tablename__
    _create_endpoint_scoped_task_claim_table(engine, table)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                INSERT INTO {table} (
                    id, provider, endpoint_id, endpoint_config_revision,
                    provider_task_ref, run_id, operation_index, operation_id, claimed_at
                ) VALUES
                    (1, 'proxmox', 17, :revision, 'UPID:first', 'run-1', 0, 'op-1', 1),
                    (2, 'proxmox', 18, :revision, 'UPID:second', 'run-2', 0, 'op-2', 2)
                """
            ),
            {"revision": "a" * 64},
        )

    database._migrate_ceph_provider_task_claim_table()
    database._migrate_ceph_provider_task_claim_table()

    inspector = inspect(engine)
    uniques = inspector.get_unique_constraints(table)
    assert any(item["column_names"] == ["provider", "provider_task_ref"] for item in uniques)
    assert all(
        item["column_names"] != ["provider", "endpoint_id", "provider_task_ref"] for item in uniques
    )
    with Session(engine) as session:
        claims = list(session.exec(select(database.CephProviderTaskClaimRecord)).all())
    assert [(claim.endpoint_id, claim.provider_task_ref) for claim in claims] == [
        (17, "UPID:first"),
        (18, "UPID:second"),
    ]
    engine.dispose()


def test_task_claim_migration_refuses_legacy_cross_endpoint_collision_without_data_loss(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'ceph-task-claim-collision.db'}")
    monkeypatch.setattr(database, "engine", engine)
    table = database.CephProviderTaskClaimRecord.__tablename__
    _create_endpoint_scoped_task_claim_table(engine, table)
    shared_ref = "UPID:node1:00000001:00000002:00000003:cephcreate:rbd:root@pam:"
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                INSERT INTO {table} (
                    id, provider, endpoint_id, endpoint_config_revision,
                    provider_task_ref, run_id, operation_index, operation_id, claimed_at
                ) VALUES
                    (1, 'proxmox', 17, :revision, :task_ref, 'run-1', 0, 'op-1', 1),
                    (2, 'proxmox', 18, :revision, :task_ref, 'run-2', 0, 'op-2', 2)
                """
            ),
            {"revision": "a" * 64, "task_ref": shared_ref},
        )

    with pytest.raises(database.CephProviderTaskClaimMigrationError) as exc_info:
        database._migrate_ceph_provider_task_claim_table()

    assert str(exc_info.value) == "ceph_provider_task_claim_cross_endpoint_collision"
    inspector = inspect(engine)
    uniques = inspector.get_unique_constraints(table)
    assert any(
        item["column_names"] == ["provider", "endpoint_id", "provider_task_ref"] for item in uniques
    )
    with engine.begin() as connection:
        rows = connection.execute(
            text(f"SELECT endpoint_id, provider_task_ref FROM {table} ORDER BY id")
        ).all()
    assert rows == [(17, shared_ref), (18, shared_ref)]
    engine.dispose()


def test_task_claim_backfill_refuses_cross_endpoint_event_collision_without_data_loss(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'ceph-task-event-collision.db'}")
    monkeypatch.setattr(database, "engine", engine)
    database.CephOperationRunRecord.__table__.create(engine)
    database.CephOperationEventRecord.__table__.create(engine)
    shared_ref = "UPID:node1:00000001:00000002:00000003:cephcreate:rbd:root@pam:"
    with Session(engine) as session:
        for index, endpoint_id in enumerate((17, 18), start=1):
            run_id = f"legacy-run-{index}"
            session.add(
                database.CephOperationRunRecord(
                    id=run_id,
                    endpoint_id=endpoint_id,
                    endpoint_config_revision="a" * 64,
                    provider="proxmox",
                    status="completed",
                )
            )
            session.add(
                database.CephOperationEventRecord(
                    run_id=run_id,
                    sequence=0,
                    operation_index=0,
                    operation_id=f"operation-{index}",
                    event="provider_task_submitted",
                    status="completed",
                    code="provider_task_submitted",
                    message="Legacy task evidence.",
                    provider_task_ref=shared_ref,
                )
            )
        session.commit()

    with pytest.raises(database.CephProviderTaskClaimMigrationError) as exc_info:
        database._migrate_ceph_provider_task_claim_table()

    assert str(exc_info.value) == "ceph_provider_task_claim_cross_endpoint_collision"
    with Session(engine) as session:
        claims = list(session.exec(select(database.CephProviderTaskClaimRecord)).all())
        events = list(session.exec(select(database.CephOperationEventRecord)).all())
    assert claims == []
    assert len(events) == 2
    assert {event.provider_task_ref for event in events} == {shared_ref}
    engine.dispose()


def test_full_startup_migration_is_idempotent_and_preserves_legacy_rows(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'full-startup.db'}")
    monkeypatch.setattr(database, "engine", engine)
    endpoint_table = database.ProxmoxEndpoint.__tablename__
    run_table = database.CephOperationRunRecord.__tablename__
    _make_legacy_proxmox_endpoint_table(engine, endpoint_table)
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {endpoint_table} "
                "(name, ip_address, username, allow_writes) "
                "VALUES ('legacy-write-enabled', '10.0.0.10', 'root@pam', 1)"
            )
        )
        conn.execute(text(f"CREATE TABLE {run_table} (id VARCHAR PRIMARY KEY)"))
        conn.execute(text(f"INSERT INTO {run_table} (id) VALUES ('legacy-run')"))

    database.create_db_and_tables()
    database.create_db_and_tables()

    with engine.begin() as conn:
        endpoints = conn.execute(
            text(f"SELECT name, allow_writes FROM {endpoint_table} ORDER BY name")
        ).all()
        legacy_run = conn.execute(
            text(f"SELECT id, status, provider FROM {run_table} WHERE id = 'legacy-run'")
        ).one()
    assert endpoints == [("legacy", 0), ("legacy-write-enabled", 1)]
    assert legacy_run == ("legacy-run", "pending", "proxmox")

    inspector = inspect(engine)
    assert inspector.has_table(database.CephOperationEventRecord.__tablename__)
    assert inspector.has_table(database.CephPlanRecord.__tablename__)
    assert inspector.has_table(database.CephApprovalRecord.__tablename__)
    assert inspector.has_table(database.CephProviderTaskClaimRecord.__tablename__)
    engine.dispose()
