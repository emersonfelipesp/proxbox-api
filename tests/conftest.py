from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlmodel import SQLModel, Session, create_engine

from proxbox_api.database import get_session
from proxbox_api.main import app
from proxbox_api.session.netbox import get_netbox_session


class FakeNetBoxSession:
    def __init__(
        self,
        *,
        status_result: Any = None,
        openapi_result: Any = None,
        status_error: Exception | None = None,
        openapi_error: Exception | None = None,
    ) -> None:
        self._status_result = status_result if status_result is not None else {"ok": True}
        self._openapi_result = openapi_result if openapi_result is not None else {
            "openapi": "3.1.0",
            "paths": {},
        }
        self._status_error = status_error
        self._openapi_error = openapi_error

    def status(self) -> Any:
        if self._status_error:
            raise self._status_error
        return self._status_result

    def openapi(self) -> Any:
        if self._openapi_error:
            raise self._openapi_error
        return self._openapi_result


@pytest.fixture(autouse=True)
def reset_fastapi_state() -> None:
    app.dependency_overrides.clear()
    app.openapi_schema = None
    yield
    app.dependency_overrides.clear()
    app.openapi_schema = None


@pytest.fixture
def db_engine(tmp_path: Path):
    sqlite_file = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{sqlite_file}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_session(db_engine):
    with Session(db_engine) as session:
        yield session


@pytest.fixture
def client_with_fake_netbox(db_engine):
    fake_session = FakeNetBoxSession(
        status_result={"status": "ok", "netbox_version": "4.2"},
        openapi_result={
            "openapi": "3.1.0",
            "paths": {"/api/virtualization/virtual-machines/": {"get": {}}},
        },
    )

    def override_get_session():
        with Session(db_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_netbox_session] = lambda: fake_session
    yield fake_session
