"""Deterministic SQLite configuration and fail-fast startup contracts."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect

from proxbox_api import database
from proxbox_api.app import bootstrap, factory
from proxbox_api.app.cors import DatabaseAwareCORSMiddleware
from proxbox_api.database import (
    DatabaseConfigurationError,
    DatabaseConfigurationSource,
    DatabaseStartupError,
    SQLiteDatabaseTarget,
    resolve_database_target,
    verify_sqlite_target,
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
        assert "netboxendpoint" in inspect(active_engine).get_table_names()
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)

    assert database.engine is None
    assert database.async_engine is None
    assert database.sqlite_file_name is None
    assert database.sqlite_url is None


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
        assert "netboxendpoint" in inspect(active_engine).get_table_names()

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
    assert {"netboxendpoint", "apikey", "api_key_bootstrap_claim"} <= tables
