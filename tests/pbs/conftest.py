"""Shared fixtures for PBS test modules."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import ApiKey, get_async_session, get_session
from proxbox_api.main import app


@pytest.fixture
def client(tmp_path: Path):
    sqlite_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{sqlite_file}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    async_url = str(engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    async_engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    def _override_get_session():
        with Session(engine) as session:
            yield session

    async def _override_get_async_session():
        async with session_factory() as session:
            yield session

    with Session(engine) as session:
        raw_key = "test-api-key-for-pbs-suite"
        ApiKey.store_key(session, raw_key, label="test-pbs")

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_async_session] = _override_get_async_session
    with TestClient(app, headers={"X-Proxbox-API-Key": raw_key}) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    asyncio.run(async_engine.dispose())
