"""Deterministic SQLite configuration and fail-fast startup contracts."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.pool import NullPool
from sqlmodel import Session

from proxbox_api import database
from proxbox_api.app import bootstrap, factory
from proxbox_api.app.cors import DatabaseAwareCORSMiddleware
from proxbox_api.database import (
    ApiKey,
    DatabaseConfigurationError,
    DatabaseConfigurationSource,
    DatabaseStartupError,
    SQLiteDatabaseTarget,
    resolve_database_target,
    verify_sqlite_target,
)
from proxbox_api.services import auth_lockout as lockout_module
from proxbox_api.services.auth_lockout import (
    LockoutConfigurationError,
    clear_runtime_auth_lockout_identity_key,
    validate_auth_lockout_identity_key,
)


def _dispose_runtime() -> None:
    asyncio.run(database.dispose_database())


def _prepare_fast_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _skip_netbox_object_bootstrap(app) -> None:  # noqa: ARG001
        return None

    monkeypatch.setenv("PROXBOX_SKIP_NETBOX_BOOTSTRAP", "1")
    monkeypatch.setattr(factory, "register_generated_proxmox_routes", lambda app: None)
    monkeypatch.setattr(factory, "_run_bootstrap_pass", _skip_netbox_object_bootstrap)
    monkeypatch.setattr(bootstrap, "_configure_backend_file_logging", lambda: None)


def _serve_distinct_loop_database_client(
    name: str,
    application: FastAPI,
    survivor: str,
    events: dict[str, dict[str, threading.Event]],
    peer_stopped: threading.Event,
    survivor_probe_done: threading.Event,
    responses: dict[str, tuple[int, dict[str, bool]]],
) -> None:
    """Serve one real client on its portal loop until the test releases it."""
    with TestClient(application) as client:
        events["ready"][name].set()
        assert events["initial_probe"][name].wait(timeout=10)
        initial = client.get("/auth/bootstrap-status")
        responses[name] = (initial.status_code, initial.json())
        events["initial_done"][name].set()
        if name == survivor:
            assert peer_stopped.wait(timeout=10)
            repeated = client.get("/auth/bootstrap-status")
            responses[f"{name}-after-peer-shutdown"] = (
                repeated.status_code,
                repeated.json(),
            )
            survivor_probe_done.set()
        assert events["release"][name].wait(timeout=10)


def _distinct_loop_client_events() -> dict[str, dict[str, threading.Event]]:
    """Create named synchronization events for both portal-thread clients."""
    return {
        "ready": {"first": threading.Event(), "second": threading.Event()},
        "initial_probe": {"first": threading.Event(), "second": threading.Event()},
        "initial_done": {"first": threading.Event(), "second": threading.Event()},
        "release": {"first": threading.Event(), "second": threading.Event()},
    }


def _start_distinct_loop_database_clients(
    executor: ThreadPoolExecutor,
    applications: dict[str, FastAPI],
    survivor: str,
    events: dict[str, dict[str, threading.Event]],
    peer_stopped: threading.Event,
    survivor_probe_done: threading.Event,
    responses: dict[str, tuple[int, dict[str, bool]]],
) -> dict[str, Future[None]]:
    """Start both portal-thread clients without adding branches to the contract test."""
    arguments = (survivor, events, peer_stopped, survivor_probe_done, responses)
    return {
        "first": executor.submit(
            _serve_distinct_loop_database_client,
            "first",
            applications["first"],
            *arguments,
        ),
        "second": executor.submit(
            _serve_distinct_loop_database_client,
            "second",
            applications["second"],
            *arguments,
        ),
    }


def _complete_initial_distinct_loop_probes(
    events: dict[str, dict[str, threading.Event]],
) -> None:
    """Probe each event loop while both client lifespans remain active."""
    assert events["ready"]["first"].wait(timeout=10)
    assert events["ready"]["second"].wait(timeout=10)
    events["initial_probe"]["first"].set()
    assert events["initial_done"]["first"].wait(timeout=10)
    events["initial_probe"]["second"].set()
    assert events["initial_done"]["second"].wait(timeout=10)


def test_resolver_uses_non_container_user_data_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database, "_legacy_default_database_candidates", tuple)
    target = resolve_database_target({"HOME": str(tmp_path)})

    expected = tmp_path / ".local" / "share" / "proxbox" / "database.db"
    assert target.path == expected
    assert target.source is DatabaseConfigurationSource.DEFAULT


def test_resolver_uses_explicit_packaged_container_default() -> None:
    target = resolve_database_target({"PROXBOX_DEFAULT_DATABASE_PATH": "/data/database.db"})

    assert target.path == Path("/data/database.db")
    assert target.source is DatabaseConfigurationSource.DEFAULT
    assert target.sync_url == "sqlite:////data/database.db"
    assert target.async_url == "sqlite+aiosqlite:////data/database.db"


def test_container_default_does_not_conflict_with_custom_database_url(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "custom-container.db"
    target = resolve_database_target(
        {
            "PROXBOX_DEFAULT_DATABASE_PATH": "/data/database.db",
            "DATABASE_URL": f"sqlite:////{str(database_path).lstrip('/')}",
        }
    )

    assert target.path == database_path
    assert target.source is DatabaseConfigurationSource.DATABASE_URL


def test_documented_non_container_default_starts_inside_user_home(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    for variable in (
        "PROXBOX_DATABASE_PATH",
        "DATABASE_URL",
        "PROXBOX_DEFAULT_DATABASE_PATH",
        "XDG_DATA_HOME",
    ):
        environment.pop(variable, None)
    environment["HOME"] = str(tmp_path)

    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from proxbox_api import database; "
                "database._legacy_default_database_candidates = tuple; "
                "from proxbox_api.database import resolve_database_target, verify_sqlite_target; "
                "target = resolve_database_target(); "
                "assert target.path == Path.home() / '.local/share/proxbox/database.db'; "
                "verify_sqlite_target(target)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    assert (tmp_path / ".local" / "share" / "proxbox" / "database.db").is_file()


@pytest.mark.parametrize("legacy_name", ("data-default.db", "cwd-default.db"))
def test_resolver_refuses_existing_legacy_implicit_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_name: str,
) -> None:
    legacy_path = tmp_path / legacy_name
    legacy_path.touch()
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )

    with pytest.raises(DatabaseConfigurationError, match="reopen API-key bootstrap"):
        resolve_database_target({"HOME": str(tmp_path / "new-home")})


@pytest.mark.parametrize("configuration", ("path", "url", "matching"))
def test_resolver_protects_explicit_fresh_targets_from_legacy_auth_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configuration: str,
) -> None:
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    selected_path = tmp_path / "selected.db"
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )
    configured_url = f"sqlite:////{str(selected_path).lstrip('/')}"
    environment = {
        "path": {"PROXBOX_DATABASE_PATH": str(selected_path)},
        "url": {"DATABASE_URL": configured_url},
        "matching": {
            "PROXBOX_DATABASE_PATH": str(selected_path),
            "DATABASE_URL": configured_url,
        },
    }[configuration]

    with pytest.raises(DatabaseConfigurationError, match="reopen API-key bootstrap"):
        resolve_database_target(environment)

    assert not selected_path.exists()


def test_resolver_accepts_auditable_fresh_database_override_for_explicit_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    selected_path = tmp_path / "selected.db"
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )

    target = resolve_database_target(
        {
            "PROXBOX_DATABASE_PATH": str(selected_path),
            "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY": "1",
            "UVICORN_WORKERS": "1",
        }
    )

    assert target.path == selected_path
    assert target.fresh_database_override is True
    assert target.legacy_database_paths == (legacy_path,)
    assert not selected_path.exists()


def test_fresh_database_override_is_audited_before_startup_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _dispose_runtime()
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    selected_path = tmp_path / "selected.db"
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )
    original_probe = database._run_sqlite_write_probe

    def _assert_audit_precedes_probe(connection, path) -> None:
        assert selected_path.with_name("selected.db.fresh-database-override-used").is_file()
        assert any(
            getattr(record, "security_override", None) == "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY"
            for record in caplog.records
        )
        original_probe(connection, path)

    monkeypatch.setattr(database, "_run_sqlite_write_probe", _assert_audit_precedes_probe)

    with caplog.at_level(logging.WARNING, logger="proxbox_api.database"):
        target = database.initialize_database_and_schema(
            {
                "PROXBOX_DATABASE_PATH": str(selected_path),
                "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY": "1",
                "UVICORN_WORKERS": "1",
            }
        )
    try:
        record = next(
            record
            for record in caplog.records
            if getattr(record, "security_override", None)
            == "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY"
        )
        assert record.database_path == str(selected_path)
        assert record.legacy_database_paths == [str(legacy_path)]
        formatted = logging.Formatter("%(levelname)s %(message)s").format(record)
        assert str(selected_path) in formatted
        assert str(legacy_path) in formatted
        assert "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY" in formatted
        assert target.fresh_database_override is True
    finally:
        _dispose_runtime()


def test_startup_warns_over_active_key_cap_and_keeps_bounded_recovery_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _dispose_runtime()
    database_path = tmp_path / "over-active-key-cap.db"
    environment = {
        "PROXBOX_DATABASE_PATH": str(database_path),
        "PROXBOX_AUTH_MAX_ACTIVE_KEYS": "1",
    }
    monkeypatch.setattr(database, "_legacy_default_database_candidates", tuple)

    database.initialize_database_and_schema(environment)
    first_key = "oldest-recovery-key-aaaaaaaaaaaaaaaaaaaaaaaa"
    with Session(database.get_engine()) as session:
        ApiKey.store_key(session, first_key, label="oldest-recovery")
        ApiKey.store_key(
            session,
            "newer-over-limit-key-bbbbbbbbbbbbbbbbbbbbbbbb",
            label="newer-over-limit",
        )
    _dispose_runtime()

    try:
        with caplog.at_level(logging.ERROR, logger="proxbox_api.database"):
            database.initialize_database_and_schema(environment)

        warning = next(
            record
            for record in caplog.records
            if getattr(record, "active_api_key_count", None) == 2
        )
        assert warning.active_api_key_limit == 1
        rendered = logging.Formatter("%(levelname)s %(message)s").format(warning)
        assert "oldest 1" in rendered
        assert "/auth/keys/{id}/deactivate" in rendered
        assert "temporarily raise PROXBOX_AUTH_MAX_ACTIVE_KEYS" in rendered

        with Session(database.get_engine()) as session:
            assert ApiKey.verify_any(session, first_key, max_active_keys=1) is True
    finally:
        _dispose_runtime()


def test_consumed_fresh_database_override_cannot_rearm_after_target_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dispose_runtime()
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    selected_path = tmp_path / "selected.db"
    environment = {
        "PROXBOX_DATABASE_PATH": str(selected_path),
        "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY": "1",
        "UVICORN_WORKERS": "1",
    }
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )

    target = database.initialize_database_and_schema(environment)
    marker_path = target.fresh_database_override_marker_path
    with sqlite3.connect(selected_path) as connection:
        connection.execute(
            "INSERT INTO apikey (label, key_hash, is_active, created_at) "
            "VALUES ('registered', 'test-hash', 1, 0)"
        )
    _dispose_runtime()

    assert marker_path.is_file()
    with pytest.raises(DatabaseConfigurationError, match="already consumed"):
        resolve_database_target(environment)

    selected_path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(f"{selected_path}{suffix}").unlink(missing_ok=True)
    with pytest.raises(DatabaseConfigurationError, match="already consumed"):
        resolve_database_target(environment)


def test_resolver_rejects_stale_override_for_copied_target_with_key_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    selected_path = tmp_path / "selected.db"
    with sqlite3.connect(selected_path) as connection:
        connection.execute("CREATE TABLE apikey (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO apikey (id) VALUES (1)")
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )

    with pytest.raises(DatabaseConfigurationError, match="already preserves"):
        resolve_database_target(
            {
                "PROXBOX_DATABASE_PATH": str(selected_path),
                "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY": "1",
                "UVICORN_WORKERS": "1",
            }
        )


@pytest.mark.parametrize(
    "worker_environment",
    ({}, {"UVICORN_WORKERS": "4"}, {"UVICORN_WORKERS": "1", "WEB_CONCURRENCY": "4"}),
)
def test_fresh_database_override_requires_explicit_single_worker_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_environment: dict[str, str],
) -> None:
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    selected_path = tmp_path / "selected.db"
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )

    with pytest.raises(DatabaseConfigurationError, match="single-worker|WORKERS=1"):
        resolve_database_target(
            {
                "PROXBOX_DATABASE_PATH": str(selected_path),
                "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY": "1",
                **worker_environment,
            }
        )

    assert not selected_path.exists()
    assert not selected_path.with_name("selected.db.fresh-database-override-used").exists()


@pytest.mark.parametrize("override", ("true", "yes", "2", "-1", " 1 "))
def test_resolver_rejects_non_exact_fresh_database_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str,
) -> None:
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )

    with pytest.raises(DatabaseConfigurationError, match="accepts only 1"):
        resolve_database_target(
            {
                "PROXBOX_DATABASE_PATH": str(tmp_path / "selected.db"),
                "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY": override,
            }
        )


def test_resolver_refuses_unnecessary_fresh_database_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database, "_legacy_default_database_candidates", tuple)

    with pytest.raises(DatabaseConfigurationError, match="unnecessary security override"):
        resolve_database_target(
            {
                "PROXBOX_DATABASE_PATH": str(tmp_path / "selected.db"),
                "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY": "1",
            }
        )


def test_resolver_fails_closed_when_legacy_candidate_cannot_be_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_path = tmp_path / "legacy.db"
    selected_path = tmp_path / "selected.db"
    original_lstat = Path.lstat

    def _permission_denied(candidate: Path):
        if candidate == legacy_path:
            raise PermissionError("synthetic EACCES")
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", _permission_denied)
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )

    with pytest.raises(DatabaseConfigurationError, match="Cannot verify legacy SQLite"):
        resolve_database_target({"PROXBOX_DATABASE_PATH": str(selected_path)})

    assert not selected_path.exists()


@pytest.mark.parametrize("history_table", ("apikey", "api_key_bootstrap_claim"))
def test_resolver_allows_explicit_target_with_durable_bootstrap_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history_table: str,
) -> None:
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    selected_path = tmp_path / "selected.db"
    with sqlite3.connect(selected_path) as connection:
        if history_table == "api_key_bootstrap_claim":
            connection.execute(
                "CREATE TABLE api_key_bootstrap_claim "
                "(id INTEGER PRIMARY KEY, initialized_at REAL NOT NULL)"
            )
            connection.execute(
                "INSERT INTO api_key_bootstrap_claim (id, initialized_at) VALUES (1, 0)"
            )
        else:
            connection.execute("CREATE TABLE apikey (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO apikey (id) VALUES (1)")
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )

    target = resolve_database_target({"PROXBOX_DATABASE_PATH": str(selected_path)})

    assert target.path == selected_path
    assert target.fresh_database_override is False
    assert target.legacy_database_paths == (legacy_path,)


def test_noncanonical_bootstrap_claim_cannot_reach_public_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    selected_path = tmp_path / "selected.db"
    with sqlite3.connect(selected_path) as connection:
        connection.execute(
            "CREATE TABLE api_key_bootstrap_claim "
            "(id INTEGER PRIMARY KEY, initialized_at REAL NOT NULL)"
        )
        connection.execute("INSERT INTO api_key_bootstrap_claim (id, initialized_at) VALUES (2, 0)")
        connection.execute(
            "CREATE TABLE apikey "
            "(id INTEGER PRIMARY KEY, label TEXT, key_hash TEXT, "
            "is_active BOOLEAN, created_at REAL)"
        )
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )

    with pytest.raises(DatabaseConfigurationError, match="noncanonical API-key"):
        resolve_database_target({"PROXBOX_DATABASE_PATH": str(selected_path)})

    with sqlite3.connect(selected_path) as connection:
        assert connection.execute("SELECT id FROM api_key_bootstrap_claim").fetchall() == [(2,)]
        assert connection.execute("SELECT COUNT(*) FROM apikey").fetchone() == (0,)


def test_incompatible_bootstrap_claim_schema_is_not_accepted_as_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    selected_path = tmp_path / "selected.db"
    with sqlite3.connect(selected_path) as connection:
        connection.execute("CREATE TABLE api_key_bootstrap_claim (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO api_key_bootstrap_claim (id) VALUES (1)")
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent.db"),
    )

    with pytest.raises(DatabaseConfigurationError, match="incompatible API-key"):
        resolve_database_target({"PROXBOX_DATABASE_PATH": str(selected_path)})


def test_dockerfile_keeps_container_default_out_of_operator_path_variable() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()

    assert "PROXBOX_DEFAULT_DATABASE_PATH=/data/database.db" in dockerfile
    assert "PROXBOX_DATABASE_PATH=/data/database.db" not in dockerfile
    assert (
        "python:3.13-alpine@sha256:"
        "540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d" in dockerfile
    )
    assert (
        dockerfile.count(
            "ghcr.io/astral-sh/uv:0.11.28@sha256:"
            "0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa"
        )
        == 3
    )
    assert "ghcr.io/astral-sh/uv:latest" not in dockerfile


@pytest.mark.parametrize(
    "driver",
    ("sqlite", "sqlite+pysqlite", "sqlite+aiosqlite"),
)
def test_resolver_accepts_supported_sqlite_database_urls(
    tmp_path: Path,
    driver: str,
) -> None:
    database_path = tmp_path / "configured.db"
    target = resolve_database_target(
        {"DATABASE_URL": f"{driver}:////{str(database_path).lstrip('/')}"}
    )

    assert target.path == database_path
    assert target.source is DatabaseConfigurationSource.DATABASE_URL
    assert target.sync_url == f"sqlite:////{str(database_path).lstrip('/')}"
    assert target.async_url == f"sqlite+aiosqlite:////{str(database_path).lstrip('/')}"


def test_resolver_accepts_matching_dual_configuration(tmp_path: Path) -> None:
    database_path = tmp_path / "matching.db"
    target = resolve_database_target(
        {
            "PROXBOX_DATABASE_PATH": str(database_path),
            "DATABASE_URL": f"sqlite:////{str(database_path).lstrip('/')}",
        }
    )

    assert target.path == database_path
    assert target.source is DatabaseConfigurationSource.MATCHING_ENVIRONMENT


def test_resolver_rejects_conflicting_dual_configuration(tmp_path: Path) -> None:
    with pytest.raises(DatabaseConfigurationError, match="select different SQLite files"):
        resolve_database_target(
            {
                "PROXBOX_DATABASE_PATH": str(tmp_path / "path.db"),
                "DATABASE_URL": f"sqlite:////{str(tmp_path / 'url.db').lstrip('/')}",
            }
        )


@pytest.mark.parametrize(
    ("environment", "message"),
    (
        ({"PROXBOX_DATABASE_PATH": "relative.db"}, "absolute SQLite file path"),
        ({"DATABASE_URL": "sqlite:///relative.db"}, "absolute SQLite file path"),
        ({"DATABASE_URL": "sqlite:///:memory:"}, "persistent absolute SQLite file"),
        ({"DATABASE_URL": "sqlite:////tmp/database.db?mode=ro"}, "query parameters"),
        ({"DATABASE_URL": "postgresql://user:secret@example/db"}, "must use sqlite"),
        ({"DATABASE_URL": "not a valid url"}, "valid absolute SQLite URL"),
    ),
)
def test_resolver_rejects_invalid_or_unsupported_configuration_without_secret_echo(
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(DatabaseConfigurationError, match=message) as captured:
        resolve_database_target(environment)

    assert "secret" not in str(captured.value)


@pytest.mark.parametrize("query", ("?", "?mode", "?mode=ro", "?production.db"))
def test_database_url_rejects_every_raw_query_delimiter_without_truncating_path(
    tmp_path: Path,
    query: str,
) -> None:
    intended_path = tmp_path / "database.db"
    truncated_path = tmp_path / "database"

    with pytest.raises(DatabaseConfigurationError, match="query delimiters"):
        resolve_database_target(
            {"DATABASE_URL": f"sqlite:////{str(intended_path).lstrip('/')}{query}"}
        )

    assert not intended_path.exists()
    assert not truncated_path.exists()


def test_write_probe_creates_only_configured_parent_and_leaves_no_probe_table(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "nested" / "state" / "database.db"
    target = SQLiteDatabaseTarget(
        path=database_path,
        source=DatabaseConfigurationSource.PROXBOX_DATABASE_PATH,
    )

    verify_sqlite_target(target)

    assert database_path.is_file()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        probe_tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name LIKE '__proxbox_startup_write_probe_%'"
        ).fetchall()
    assert probe_tables == []


def test_write_probe_rejects_existing_read_only_parent_even_as_root(tmp_path: Path) -> None:
    parent = tmp_path / "read-only"
    parent.mkdir()
    parent.chmod(0o555)
    try:
        target = SQLiteDatabaseTarget(
            path=parent / "database.db",
            source=DatabaseConfigurationSource.PROXBOX_DATABASE_PATH,
        )
        with pytest.raises(DatabaseStartupError, match="directory is read-only"):
            verify_sqlite_target(target)
        assert not target.path.exists()
    finally:
        parent.chmod(0o755)


def test_write_probe_rejects_existing_read_only_database_even_as_root(tmp_path: Path) -> None:
    database_path = tmp_path / "read-only.db"
    database_path.touch()
    database_path.chmod(0o444)
    try:
        target = SQLiteDatabaseTarget(
            path=database_path,
            source=DatabaseConfigurationSource.PROXBOX_DATABASE_PATH,
        )
        with pytest.raises(DatabaseStartupError, match="file is read-only"):
            verify_sqlite_target(target)
    finally:
        database_path.chmod(0o644)


def test_write_probe_rejects_filesystem_that_cannot_enable_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_connect = sqlite3.connect

    def _memory_connection(*args, **kwargs):  # noqa: ANN002, ANN003
        return real_connect(":memory:", timeout=5.0, isolation_level=None)

    monkeypatch.setattr(database.sqlite3, "connect", _memory_connection)
    target = SQLiteDatabaseTarget(
        path=tmp_path / "wal-required.db",
        source=DatabaseConfigurationSource.PROXBOX_DATABASE_PATH,
    )

    with pytest.raises(DatabaseStartupError, match="did not enable WAL mode"):
        verify_sqlite_target(target)


def test_write_probe_translates_sqlite_write_failure_without_raw_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_write(connection, path) -> None:  # noqa: ARG001
        raise sqlite3.OperationalError("sensitive-driver-detail")

    monkeypatch.setattr(database, "_run_sqlite_write_probe", _fail_write)
    target = SQLiteDatabaseTarget(
        path=tmp_path / "write-failure.db",
        source=DatabaseConfigurationSource.PROXBOX_DATABASE_PATH,
    )

    with pytest.raises(DatabaseStartupError, match="not writable with WAL") as captured:
        verify_sqlite_target(target)
    assert "sensitive-driver-detail" not in str(captured.value)


def test_application_import_does_not_resolve_database_configuration() -> None:
    environment = os.environ.copy()
    environment["PROXBOX_DATABASE_PATH"] = "relative.db"
    environment.pop("DATABASE_URL", None)

    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from proxbox_api import database; "
                "import proxbox_api.main; "
                "assert database.engine is None; "
                "assert database.async_engine is None"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )


def test_application_construction_does_not_resolve_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dispose_runtime()
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", "relative.db")

    application = factory.create_app()

    assert application is not None
    assert database.sqlite_file_name is None
    assert database.sqlite_url is None
    assert database.engine is None
    assert database.async_engine is None


def test_lifespan_reestablishes_identity_key_after_previous_owner_disposes() -> None:
    _dispose_runtime()
    application = factory.create_app()
    clear_runtime_auth_lockout_identity_key()

    with TestClient(application):
        validate_auth_lockout_identity_key()

    with pytest.raises(
        LockoutConfigurationError,
        match="authentication lockout identity key was not validated during startup",
    ):
        validate_auth_lockout_identity_key()


def test_database_aware_cors_uses_endpoints_loaded_during_lifespan() -> None:
    async def _app(scope, receive, send) -> None:  # noqa: ARG001
        return None

    endpoint = SimpleNamespace(domain="netbox.example", port=443, verify_ssl=True)
    middleware = DatabaseAwareCORSMiddleware(
        _app,
        endpoint_provider=lambda: [endpoint],
        allow_origins=[],
    )

    assert middleware.is_allowed_origin("https://netbox.example") is True
    assert middleware.is_allowed_origin("https://not-configured.example") is False


async def test_lifespan_fails_before_serving_on_invalid_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", "relative.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    application = factory.create_app()

    with pytest.raises(DatabaseConfigurationError, match="absolute SQLite file path"):
        async with factory._lifespan(application):
            pytest.fail("The application served traffic with invalid database configuration")

    assert database.engine is None


async def test_lifespan_fails_before_serving_on_existing_unwritable_data_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    parent = tmp_path / "data"
    parent.mkdir()
    parent.chmod(0o555)
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(parent / "database.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    application = factory.create_app()

    try:
        with pytest.raises(DatabaseStartupError, match="directory is read-only"):
            async with factory._lifespan(application):
                pytest.fail("The application served traffic with an unwritable database")
    finally:
        parent.chmod(0o755)

    assert database.engine is None


async def test_lifespan_fails_when_required_migration_inspection_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    database_path = tmp_path / "migration-inspection.db"
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def _inspection_failure(engine) -> None:  # noqa: ARG001
        raise OSError("synthetic schema inspection failure")

    monkeypatch.setattr(database, "inspect", _inspection_failure)
    application = factory.create_app()

    with pytest.raises(DatabaseStartupError, match="Failed to inspect SQLite schema"):
        async with factory._lifespan(application):
            pytest.fail("The application served traffic after migration inspection failed")

    assert database.engine is None


def test_bootstrap_propagates_required_endpoint_table_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingSession:
        closed = False

        def exec(self, statement):  # noqa: ANN001, ARG002
            raise sqlite3.OperationalError("synthetic endpoint-table read failure")

        def close(self) -> None:
            self.closed = True

    target = SQLiteDatabaseTarget(
        path=tmp_path / "database.db",
        source=DatabaseConfigurationSource.PROXBOX_DATABASE_PATH,
    )
    session = _FailingSession()
    monkeypatch.setattr(bootstrap, "initialize_database_and_schema", lambda: target)
    monkeypatch.setattr(bootstrap, "get_session", lambda: iter((session,)))

    with pytest.raises(DatabaseStartupError, match="post-schema endpoint read"):
        bootstrap.init_database_and_netbox()

    assert session.closed is True
    assert bootstrap.init_ok is False


async def test_lifespan_refuses_legacy_database_before_creating_empty_bootstrap_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    legacy_path = tmp_path / "legacy-data" / "database.db"
    legacy_path.parent.mkdir()
    with sqlite3.connect(legacy_path) as connection:
        connection.execute("CREATE TABLE preserved_control_plane (value TEXT NOT NULL)")
        connection.execute("INSERT INTO preserved_control_plane VALUES ('existing')")
    new_home = tmp_path / "new-home"
    new_default = new_home / ".local" / "share" / "proxbox" / "database.db"
    monkeypatch.setattr(
        database,
        "_legacy_default_database_candidates",
        lambda: (legacy_path, tmp_path / "absent-cwd.db"),
    )
    monkeypatch.setenv("HOME", str(new_home))
    for variable in (
        "PROXBOX_DATABASE_PATH",
        "DATABASE_URL",
        "PROXBOX_DEFAULT_DATABASE_PATH",
        "XDG_DATA_HOME",
    ):
        monkeypatch.delenv(variable, raising=False)
    application = factory.create_app()

    with pytest.raises(DatabaseConfigurationError, match="reopen API-key bootstrap"):
        async with factory._lifespan(application):
            pytest.fail("The application bypassed an existing control-plane database")

    assert not new_default.exists()
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute("SELECT value FROM preserved_control_plane").fetchone() == (
            "existing",
        )
    assert database.engine is None


async def test_lifespan_builds_verified_engines_and_tables_then_disposes_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    database_path = tmp_path / "startup" / "database.db"
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    application = factory.create_app()

    assert database.engine is None
    async with factory._lifespan(application):
        active_engine = database.get_engine()
        assert database.sqlite_file_name == database_path
        assert database.sqlite_url == f"sqlite:////{str(database_path).lstrip('/')}"
        assert active_engine.url.database == str(database_path)
        table_names = inspect(active_engine).get_table_names()
        assert "netboxendpoint" in table_names
        assert "browser_console_relay_session" in table_names
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)

    assert database.engine is None
    assert database.async_engine is None
    assert database.sqlite_file_name is None
    assert database.sqlite_url is None


async def test_overlapping_lifespans_dispose_only_after_the_final_owner_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    database_path = tmp_path / "shared-runtime.db"
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    first_lifespan = factory._lifespan(factory.create_app())
    second_lifespan = factory._lifespan(factory.create_app())

    await first_lifespan.__aenter__()
    first_active = True
    second_active = False
    try:
        first_engine = database.get_engine()
        await second_lifespan.__aenter__()
        second_active = True
        assert database.get_engine() is first_engine

        await first_lifespan.__aexit__(None, None, None)
        first_active = False
        assert database.get_engine() is first_engine
        validate_auth_lockout_identity_key()
    finally:
        if second_active:
            await second_lifespan.__aexit__(None, None, None)
        if first_active:
            await first_lifespan.__aexit__(None, None, None)

    assert database.engine is None
    with pytest.raises(
        LockoutConfigurationError,
        match="authentication lockout identity key was not validated during startup",
    ):
        validate_auth_lockout_identity_key()


@pytest.mark.parametrize("first_to_stop", ("first", "second"))
def test_overlapping_test_clients_share_async_runtime_across_event_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_to_stop: str,
) -> None:
    _dispose_runtime()
    _prepare_fast_lifespan(monkeypatch)
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / f"loops-{first_to_stop}.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    applications = {"first": factory.create_app(), "second": factory.create_app()}
    events = _distinct_loop_client_events()
    peer_stopped = threading.Event()
    survivor_probe_done = threading.Event()
    survivor = "second" if first_to_stop == "first" else "first"
    responses: dict[str, tuple[int, dict[str, bool]]] = {}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = _start_distinct_loop_database_clients(
            executor,
            applications,
            survivor,
            events,
            peer_stopped,
            survivor_probe_done,
            responses,
        )
        _complete_initial_distinct_loop_probes(events)
        candidate_async_engine = database.async_engine
        assert candidate_async_engine is not None
        assert isinstance(candidate_async_engine.sync_engine.pool, NullPool)

        events["release"][first_to_stop].set()
        futures[first_to_stop].result(timeout=10)
        peer_stopped.set()
        assert survivor_probe_done.wait(timeout=10)
        events["release"][survivor].set()
        futures[survivor].result(timeout=10)

    expected = (200, {"needs_bootstrap": True, "has_db_keys": False})
    assert responses == {
        "first": expected,
        "second": expected,
        f"{survivor}-after-peer-shutdown": expected,
    }
    assert database.engine is None
    assert database.async_engine is None


async def test_simultaneous_owners_publish_bootstrap_globals_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / "simultaneous.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    first_owner = await database.acquire_database_runtime()
    second_owner = await database.acquire_database_runtime()
    original_initialize = bootstrap._initialize_database_and_netbox
    initialization_started = threading.Event()
    allow_initialization = threading.Event()
    initialization_calls = 0

    def _slow_initialize(owner) -> None:  # noqa: ANN001
        nonlocal initialization_calls
        initialization_calls += 1
        initialization_started.set()
        assert allow_initialization.wait(timeout=5)
        original_initialize(owner)

    monkeypatch.setattr(bootstrap, "_initialize_database_and_netbox", _slow_initialize)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(bootstrap.init_database_and_netbox, first_owner)
            assert initialization_started.wait(timeout=5)
            second = executor.submit(bootstrap.init_database_and_netbox, second_owner)
            assert not second.done()
            allow_initialization.set()
            first.result(timeout=10)
            second.result(timeout=10)

        assert initialization_calls == 1
        assert bootstrap.init_ok is True
        validate_auth_lockout_identity_key()
    finally:
        allow_initialization.set()
        await database.release_database_runtime(first_owner)
        await database.release_database_runtime(second_owner)


async def test_failed_bootstrap_wakes_waiters_and_next_generation_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / "failed-generation.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    first_owner = await database.acquire_database_runtime()
    second_owner = await database.acquire_database_runtime()
    original_initialize = bootstrap._initialize_database_and_netbox
    initialization_started = threading.Event()
    allow_failure = threading.Event()

    def _fail_initialize(owner) -> None:  # noqa: ANN001
        initialization_started.set()
        assert allow_failure.wait(timeout=5)
        raise DatabaseStartupError(f"synthetic shared failure for {owner.generation}")

    monkeypatch.setattr(bootstrap, "_initialize_database_and_netbox", _fail_initialize)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(bootstrap.init_database_and_netbox, first_owner)
        assert initialization_started.wait(timeout=5)
        second = executor.submit(bootstrap.init_database_and_netbox, second_owner)
        assert not second.done()
        allow_failure.set()
        with pytest.raises(DatabaseStartupError, match="synthetic shared failure"):
            first.result(timeout=10)
        with pytest.raises(DatabaseStartupError, match="bootstrap already failed"):
            second.result(timeout=10)

    failed_generation = first_owner.generation
    await database.release_database_runtime(first_owner)
    await database.release_database_runtime(second_owner)
    assert database._database_runtime_owners == {}
    assert database.engine is None

    monkeypatch.setattr(bootstrap, "_initialize_database_and_netbox", original_initialize)
    replacement_owner = await database.acquire_database_runtime()
    try:
        assert replacement_owner.generation != failed_generation
        bootstrap.init_database_and_netbox(replacement_owner)
        assert bootstrap._bootstrap_ready_generation == replacement_owner.generation
        assert bootstrap._bootstrap_failed_generation is None
        assert bootstrap.init_ok is True
    finally:
        await database.release_database_runtime(replacement_owner)


async def test_secondary_lifespan_does_not_rerun_successful_shared_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    database_path = tmp_path / "shared-bootstrap.db"
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    first_lifespan = factory._lifespan(factory.create_app())
    second_lifespan = factory._lifespan(factory.create_app())

    await first_lifespan.__aenter__()
    second_active = False
    try:
        first_engine = database.get_engine()

        def _unexpected_secondary_bootstrap(owner) -> None:  # noqa: ANN001
            raise DatabaseStartupError("secondary bootstrap corrupted shared globals")

        monkeypatch.setattr(
            bootstrap,
            "_initialize_database_and_netbox",
            _unexpected_secondary_bootstrap,
        )
        await second_lifespan.__aenter__()
        second_active = True

        assert database.get_engine() is first_engine
        assert bootstrap.init_ok is True
        validate_auth_lockout_identity_key()
    finally:
        if second_active:
            await second_lifespan.__aexit__(None, None, None)
        await first_lifespan.__aexit__(None, None, None)


async def test_secondary_same_target_owner_does_not_reload_identity_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    database_path = tmp_path / "shared-identity.db"
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    first_lifespan = factory._lifespan(factory.create_app())
    second_lifespan = factory._lifespan(factory.create_app())

    await first_lifespan.__aenter__()
    second_active = False
    try:
        first_engine = database.get_engine()

        def _unexpected_identity_reload(expected_fingerprint) -> str:  # noqa: ANN001
            raise LockoutConfigurationError(
                f"secondary owner reloaded identity {expected_fingerprint}"
            )

        monkeypatch.setattr(
            lockout_module,
            "initialize_auth_lockout_identity_key",
            _unexpected_identity_reload,
        )
        await second_lifespan.__aenter__()
        second_active = True

        assert database.get_engine() is first_engine
        validate_auth_lockout_identity_key()
    finally:
        if second_active:
            await second_lifespan.__aexit__(None, None, None)
        await first_lifespan.__aexit__(None, None, None)


async def test_repeated_cancellation_while_acquisition_waits_leaves_no_ghost_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / "cancelled-acquisition.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    original_claim = database._claim_database_runtime
    claim_started = threading.Event()

    def _observed_claim(environ):  # noqa: ANN001
        claim_started.set()
        return original_claim(environ)

    monkeypatch.setattr(database, "_claim_database_runtime", _observed_claim)
    with database._database_runtime_condition:
        database._database_runtime_transitioning = True
    acquisition = asyncio.create_task(database.acquire_database_runtime())
    assert await asyncio.to_thread(claim_started.wait, 5)
    acquisition.cancel()
    await asyncio.sleep(0)
    acquisition.cancel()
    with database._database_runtime_condition:
        database._database_runtime_transitioning = False
        database._database_runtime_condition.notify_all()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(acquisition, timeout=10)
    assert database._database_runtime_owners == {}
    assert database._database_runtime_transitioning is False
    assert database.engine is None


async def test_repeated_cancellation_waits_for_sync_then_async_disposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / "cancelled-sync-disposal.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    owner = await database.acquire_database_runtime()
    sync_engine = database.engine
    candidate_async_engine = database.async_engine
    assert sync_engine is not None
    assert candidate_async_engine is not None
    original_sync_dispose = type(sync_engine).dispose
    original_async_dispose = type(candidate_async_engine).dispose
    sync_started = threading.Event()
    allow_sync = threading.Event()
    async_started = asyncio.Event()

    def _blocked_sync_dispose(engine, *args, **kwargs) -> None:  # noqa: ANN001
        if engine is sync_engine:
            sync_started.set()
            assert allow_sync.wait(timeout=5)
        original_sync_dispose(engine, *args, **kwargs)

    async def _observed_async_dispose(engine) -> None:  # noqa: ANN001
        async_started.set()
        await original_async_dispose(engine)

    monkeypatch.setattr(type(sync_engine), "dispose", _blocked_sync_dispose)
    monkeypatch.setattr(type(candidate_async_engine), "dispose", _observed_async_dispose)
    release = asyncio.create_task(database.release_database_runtime(owner))
    assert await asyncio.to_thread(sync_started.wait, 5)
    release.cancel()
    await asyncio.sleep(0)
    release.cancel()
    await asyncio.sleep(0)
    assert not release.done()
    assert not async_started.is_set()

    allow_sync.set()
    await asyncio.wait_for(async_started.wait(), timeout=5)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(release, timeout=10)
    assert database._database_runtime_transitioning is False
    assert database._database_runtime_poisoned is None


async def test_final_release_holds_runtime_lease_until_cancelled_async_disposal_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    database_path = tmp_path / "ordered-disposal.db"
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    owner = await database.acquire_database_runtime()
    candidate_async_engine = database.async_engine
    assert candidate_async_engine is not None
    original_dispose = type(candidate_async_engine).dispose
    disposal_started = asyncio.Event()
    allow_disposal = asyncio.Event()

    async def _blocked_dispose(engine) -> None:  # noqa: ANN001
        disposal_started.set()
        await allow_disposal.wait()
        await original_dispose(engine)

    def _assert_offline_maintenance_refused() -> None:
        with pytest.raises(DatabaseStartupError, match="worker still holds the runtime lease"):
            with database.offline_database_maintenance_lock(database_path):
                pytest.fail("Offline maintenance acquired a live runtime lease")

    monkeypatch.setattr(type(candidate_async_engine), "dispose", _blocked_dispose)
    release = asyncio.create_task(database.release_database_runtime(owner))
    await asyncio.wait_for(disposal_started.wait(), timeout=5)
    replacement = asyncio.create_task(database.acquire_database_runtime())
    await asyncio.sleep(0.05)
    assert not replacement.done()
    await asyncio.to_thread(_assert_offline_maintenance_refused)

    release.cancel()
    await asyncio.sleep(0)
    release.cancel()
    await asyncio.sleep(0)
    assert not release.done()
    await asyncio.to_thread(_assert_offline_maintenance_refused)

    allow_disposal.set()
    with pytest.raises(asyncio.CancelledError):
        await release
    replacement_owner = await asyncio.wait_for(replacement, timeout=10)
    await database.release_database_runtime(replacement_owner)


async def test_saturated_default_executor_cannot_deadlock_runtime_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / "saturated-executor.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    loop = asyncio.get_running_loop()
    one_worker_executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(one_worker_executor)
    owner = await database.acquire_database_runtime()
    candidate_async_engine = database.async_engine
    assert candidate_async_engine is not None
    original_dispose = type(candidate_async_engine).dispose
    disposal_started = asyncio.Event()
    allow_disposal = asyncio.Event()
    default_worker_started = threading.Event()
    allow_default_worker = threading.Event()

    async def _blocked_dispose(engine) -> None:  # noqa: ANN001
        if engine is candidate_async_engine:
            disposal_started.set()
            await allow_disposal.wait()
        await original_dispose(engine)

    def _occupy_default_worker() -> None:
        default_worker_started.set()
        assert allow_default_worker.wait(timeout=10)

    monkeypatch.setattr(type(candidate_async_engine), "dispose", _blocked_dispose)
    release = asyncio.create_task(database.release_database_runtime(owner))
    default_blocker: asyncio.Future[None] | None = None
    replacement: asyncio.Task[database.DatabaseRuntimeOwner] | None = None
    try:
        await asyncio.wait_for(disposal_started.wait(), timeout=5)
        default_blocker = loop.run_in_executor(None, _occupy_default_worker)
        while not default_worker_started.is_set():
            await asyncio.sleep(0)
        replacement = asyncio.create_task(database.acquire_database_runtime())
        await asyncio.sleep(0.05)
        assert not replacement.done()

        allow_disposal.set()
        await asyncio.wait_for(release, timeout=10)
        replacement_owner = await asyncio.wait_for(replacement, timeout=10)
        await database.release_database_runtime(replacement_owner)
    finally:
        allow_disposal.set()
        allow_default_worker.set()
        if default_blocker is not None:
            await default_blocker
        one_worker_executor.shutdown(wait=True)


async def test_repeated_cancellation_waits_for_transition_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / "cancelled-finalization.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    owner = await database.acquire_database_runtime()
    original_finish = database._finish_database_runtime_transition
    finish_started = threading.Event()
    allow_finish = threading.Event()

    def _blocked_finish(runtime_lease: int | None) -> None:
        finish_started.set()
        assert allow_finish.wait(timeout=5)
        original_finish(runtime_lease)

    monkeypatch.setattr(database, "_finish_database_runtime_transition", _blocked_finish)
    release = asyncio.create_task(database.release_database_runtime(owner))
    assert await asyncio.to_thread(finish_started.wait, 5)
    replacement = asyncio.create_task(database.acquire_database_runtime())
    await asyncio.sleep(0.05)
    assert not replacement.done()
    release.cancel()
    await asyncio.sleep(0)
    release.cancel()
    await asyncio.sleep(0)
    assert not release.done()
    assert not replacement.done()

    allow_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(release, timeout=10)
    replacement_owner = await asyncio.wait_for(replacement, timeout=10)
    await database.release_database_runtime(replacement_owner)


@pytest.mark.parametrize(
    ("fail_sync", "fail_async"),
    ((True, False), (False, True), (True, True)),
)
async def test_disposal_failure_attempts_both_engines_and_poisons_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_sync: bool,
    fail_async: bool,
) -> None:
    await database.dispose_database()
    database_path = tmp_path / f"poisoned-{fail_sync}-{fail_async}.db"
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    owner = await database.acquire_database_runtime()
    sync_engine = database.engine
    candidate_async_engine = database.async_engine
    assert sync_engine is not None
    assert candidate_async_engine is not None
    original_sync_dispose = type(sync_engine).dispose
    original_async_dispose = type(candidate_async_engine).dispose
    attempts = {"sync": 0, "async": 0}

    def _sync_dispose(engine, *args, **kwargs) -> None:  # noqa: ANN001
        if engine is sync_engine:
            attempts["sync"] += 1
            if fail_sync:
                raise RuntimeError("synthetic synchronous disposal failure")
        original_sync_dispose(engine, *args, **kwargs)

    async def _async_dispose(engine) -> None:  # noqa: ANN001
        attempts["async"] += 1
        if fail_async:
            raise RuntimeError("synthetic asynchronous disposal failure")
        await original_async_dispose(engine)

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(type(sync_engine), "dispose", _sync_dispose)
        failure_patch.setattr(type(candidate_async_engine), "dispose", _async_dispose)
        with pytest.raises(DatabaseStartupError, match="restart it") as failure:
            await database.release_database_runtime(owner)

    try:
        assert attempts == {"sync": 1, "async": 1}
        assert database._database_runtime_poisoned is not None
        expected_notes = int(fail_sync) + int(fail_async)
        assert len(failure.value.__notes__) == expected_notes
        validate_auth_lockout_identity_key()
        with pytest.raises(DatabaseStartupError, match="cleanup previously failed"):
            await database.acquire_database_runtime()

        def _assert_offline_maintenance_refused() -> None:
            with pytest.raises(DatabaseStartupError, match="worker still holds"):
                with database.offline_database_maintenance_lock(database_path):
                    pytest.fail("Offline maintenance acquired a poisoned runtime lease")

        await asyncio.to_thread(_assert_offline_maintenance_refused)
    finally:
        original_sync_dispose(sync_engine)
        await original_async_dispose(candidate_async_engine)
        runtime_lease = database._database_runtime_lease_descriptor
        database._database_runtime_lease_descriptor = None
        database._database_runtime_poisoned = None
        database._database_runtime_transitioning = False
        database._database_runtime_owners.clear()
        database._release_database_runtime_lease(runtime_lease)
        clear_runtime_auth_lockout_identity_key()


@pytest.mark.parametrize("failure_site", ("startup", "body"))
async def test_lifespan_preserves_primary_failure_when_release_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / f"primary-{failure_site}.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    original_release = database.release_database_runtime

    async def _release_then_fail(owner) -> None:  # noqa: ANN001
        await original_release(owner)
        raise DatabaseStartupError("synthetic release failure")

    monkeypatch.setattr(database, "release_database_runtime", _release_then_fail)
    if failure_site == "startup":

        def _fail_startup(owner) -> None:  # noqa: ANN001
            raise ValueError("primary startup failure")

        monkeypatch.setattr(bootstrap, "init_database_and_netbox", _fail_startup)

    with pytest.raises(ValueError, match=f"primary {failure_site} failure") as failure:
        async with factory._lifespan(factory.create_app()):
            if failure_site == "body":
                raise ValueError("primary body failure")

    assert failure.value.__notes__ == [
        "Database runtime cleanup also failed: DatabaseStartupError: synthetic release failure"
    ]
    assert database._database_runtime_owners == {}
    assert database.engine is None


async def test_failed_conflicting_lifespan_does_not_release_the_incumbent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    first_path = tmp_path / "incumbent.db"
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(first_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    first_lifespan = factory._lifespan(factory.create_app())

    await first_lifespan.__aenter__()
    try:
        first_engine = database.get_engine()
        monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / "conflicting.db"))
        with pytest.raises(DatabaseStartupError, match="different SQLite target"):
            async with factory._lifespan(factory.create_app()):
                pytest.fail("The conflicting application unexpectedly entered its lifespan")

        assert database.get_engine() is first_engine
        assert database.database_target is not None
        assert database.database_target.path == first_path
        validate_auth_lockout_identity_key()
    finally:
        await first_lifespan.__aexit__(None, None, None)

    assert database.engine is None


async def test_failed_owned_startup_releases_only_its_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    database_path = tmp_path / "owned-startup.db"
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    first_lifespan = factory._lifespan(factory.create_app())

    await first_lifespan.__aenter__()
    try:
        first_engine = database.get_engine()

        def _fail_bootstrap(runtime_owner) -> None:  # noqa: ANN001
            assert runtime_owner.target.path == database_path
            raise DatabaseStartupError("synthetic owned bootstrap failure")

        monkeypatch.setattr(bootstrap, "init_database_and_netbox", _fail_bootstrap)
        with pytest.raises(DatabaseStartupError, match="synthetic owned bootstrap failure"):
            async with factory._lifespan(factory.create_app()):
                pytest.fail("The failed application unexpectedly entered its lifespan")

        assert database.get_engine() is first_engine
        validate_auth_lockout_identity_key()
    finally:
        await first_lifespan.__aexit__(None, None, None)

    assert database.engine is None


async def test_direct_disposal_refuses_an_owned_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(tmp_path / "owned-runtime.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    async with factory._lifespan(factory.create_app()):
        active_engine = database.get_engine()
        with pytest.raises(DatabaseStartupError, match="lifespans own it"):
            await database.dispose_database()
        assert database.get_engine() is active_engine

    assert database.engine is None


async def test_lifespan_preserves_url_delimiters_inside_database_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await database.dispose_database()
    _prepare_fast_lifespan(monkeypatch)
    database_path = tmp_path / "database?production.db"
    truncated_path = tmp_path / "database"
    monkeypatch.setenv("PROXBOX_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    application = factory.create_app()

    async with factory._lifespan(application):
        active_engine = database.get_engine()
        assert active_engine.url.database == str(database_path)
        assert database.async_engine is not None
        assert database.async_engine.url.database == str(database_path)
        table_names = inspect(active_engine).get_table_names()
        assert "netboxendpoint" in table_names
        assert "browser_console_relay_session" in table_names

    assert database_path.is_file()
    assert not truncated_path.exists()


def test_four_process_override_rejects_multi_worker_recovery_before_writes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "database.db"
    legacy_path = tmp_path / "legacy.db"
    legacy_path.touch()
    child_code = """
import sys
from pathlib import Path

from proxbox_api import database

selected_path = Path(sys.argv[1])
legacy_path = Path(sys.argv[2])
database._legacy_default_database_candidates = lambda: (legacy_path,)
try:
    database.resolve_database_target(
        {
            "PROXBOX_DATABASE_PATH": str(selected_path),
            "PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY": "1",
            "UVICORN_WORKERS": "4",
        }
    )
except database.DatabaseConfigurationError as error:
    assert "UVICORN_WORKERS=1" in str(error)
else:
    raise AssertionError("multi-worker override unexpectedly resolved")
"""
    processes = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", child_code, str(database_path), str(legacy_path)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]

    results = [process.communicate(timeout=30) for process in processes]
    failures = [
        {"worker": index, "stdout": stdout, "stderr": stderr}
        for index, (process, (stdout, stderr)) in enumerate(zip(processes, results))
        if process.returncode != 0
    ]
    assert failures == []
    assert not database_path.exists()
    assert not database_path.with_name("database.db.fresh-database-override-used").exists()


def test_four_processes_serialize_probe_schema_and_all_migrations(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shared" / "database.db"
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    release_path = tmp_path / "release"
    boundary_marker = tmp_path / "startup-boundary-active"
    child_code = """
import asyncio
import os
import sys
import time
from pathlib import Path

from proxbox_api import database

database_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
release_path = Path(sys.argv[3])
boundary_marker = Path(sys.argv[4])
database._legacy_default_database_candidates = tuple
os.environ["PROXBOX_DATABASE_PATH"] = str(database_path)
os.environ.pop("DATABASE_URL", None)

original_probe = database._run_sqlite_write_probe
original_create = database._create_db_and_tables_unlocked
owns_boundary = False

def instrumented_probe(connection, path):
    global owns_boundary
    boundary_marker.mkdir()
    owns_boundary = True
    try:
        original_probe(connection, path)
    except BaseException:
        boundary_marker.rmdir()
        owns_boundary = False
        raise

def instrumented_create():
    global owns_boundary
    assert owns_boundary
    assert boundary_marker.is_dir()
    time.sleep(0.15)
    try:
        original_create()
    finally:
        boundary_marker.rmdir()
        owns_boundary = False

database._run_sqlite_write_probe = instrumented_probe
database._create_db_and_tables_unlocked = instrumented_create
ready_path.touch()
deadline = time.monotonic() + 30
while not release_path.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("parent did not release startup barrier")
    time.sleep(0.01)

target = database.initialize_database_and_schema()
assert target.path == database_path
asyncio.run(database.dispose_database())
"""
    processes = [
        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-c",
                child_code,
                str(database_path),
                str(ready_dir / str(index)),
                str(release_path),
                str(boundary_marker),
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(4)
    ]

    try:
        deadline = time.monotonic() + 30
        while len(tuple(ready_dir.iterdir())) != len(processes):
            if time.monotonic() >= deadline:
                pytest.fail("four startup workers did not reach the release barrier")
            time.sleep(0.01)
        release_path.touch()

        results = [process.communicate(timeout=90) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

    failures = [
        {
            "worker": index,
            "returncode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
        for index, (process, (stdout, stderr)) in enumerate(zip(processes, results))
        if process.returncode != 0
    ]
    assert failures == []
    assert not boundary_marker.exists()
    assert database_path.with_name("database.db.startup.lock").is_file()
    with sqlite3.connect(database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "netboxendpoint",
        "apikey",
        "api_key_bootstrap_claim",
        "browser_console_relay_session",
    } <= tables
