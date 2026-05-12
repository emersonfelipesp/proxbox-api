"""SQLModel database configuration and endpoint models."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Annotated

import bcrypt
from fastapi import Depends
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import Field, Session, SQLModel, create_engine, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.credentials import decrypt_value, encrypt_value

root_dir = Path(__file__).parent.parent
sqlite_file_name = root_dir / "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"
async_sqlite_url = f"sqlite+aiosqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args, poolclass=NullPool)

async_engine = create_async_engine(async_sqlite_url, connect_args=connect_args)
async_session_factory = async_sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False
)


class NetBoxEndpoint(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    ip_address: str = Field(index=True)
    domain: str = Field(index=True)
    port: int = Field(default=443)
    token_version: str = Field(default="v1")
    token_key: str | None = Field(default=None)
    token: str = Field()
    verify_ssl: bool = Field(default=True)

    @property
    def url(self) -> str:
        protocol = "https" if self.port == 443 or self.verify_ssl else "http"
        host = self.domain if self.domain else self.ip_address.split("/")[0]
        return f"{protocol}://{host}:{self.port}"

    def get_decrypted_token(self) -> str:
        return decrypt_value(self.token) or ""

    def get_decrypted_token_key(self) -> str | None:
        return decrypt_value(self.token_key)

    def set_encrypted_token(self, value: str) -> None:
        self.token = encrypt_value(value) or value

    def set_encrypted_token_key(self, value: str | None) -> None:
        self.token_key = encrypt_value(value)


class ProxmoxEndpoint(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    ip_address: str = Field(index=True)
    domain: str | None = Field(default=None, index=True)
    port: int = Field(default=8006)
    username: str = Field(index=True)
    password: str | None = Field(default=None)
    verify_ssl: bool = Field(default=True)
    allow_writes: bool = Field(default=False)
    token_name: str | None = Field(default=None)
    token_value: str | None = Field(default=None)
    timeout: int | None = Field(default=None)
    max_retries: int | None = Field(default=None)
    retry_backoff: float | None = Field(default=None)
    site_id: int | None = Field(default=None)
    site_slug: str | None = Field(default=None)
    site_name: str | None = Field(default=None)
    tenant_id: int | None = Field(default=None)
    tenant_slug: str | None = Field(default=None)
    tenant_name: str | None = Field(default=None)

    @property
    def has_token(self) -> bool:
        return bool(self.token_name and self.token_value)

    @property
    def host(self) -> str:
        return self.domain or self.ip_address

    def get_decrypted_password(self) -> str | None:
        return decrypt_value(self.password)

    def get_decrypted_token_value(self) -> str | None:
        return decrypt_value(self.token_value)

    def set_encrypted_password(self, value: str | None) -> None:
        self.password = encrypt_value(value)

    def set_encrypted_token_value(self, value: str | None) -> None:
        self.token_value = encrypt_value(value)


class AuthLockout(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    ip_address: str = Field(primary_key=True)
    attempts: int = Field(default=0)
    first_attempt_time: float = Field(default=0)

    @staticmethod
    def is_locked_out(
        session: Session, ip: str, max_attempts: int = 5, lockout_duration: float = 300
    ) -> bool:
        lockout = session.get(AuthLockout, ip)
        if not lockout:
            return False
        if lockout.attempts >= max_attempts:
            if time.time() - lockout.first_attempt_time < lockout_duration:
                return True
            session.delete(lockout)
            session.commit()
        return False

    @staticmethod
    def record_failed_attempt(session: Session, ip: str) -> None:
        lockout = session.get(AuthLockout, ip)
        now = time.time()
        if not lockout:
            lockout = AuthLockout(ip_address=ip, attempts=1, first_attempt_time=now)
            session.add(lockout)
        else:
            if now - lockout.first_attempt_time > 300:
                lockout.attempts = 1
                lockout.first_attempt_time = now
            else:
                lockout.attempts += 1
        session.commit()

    @staticmethod
    def clear_failed_attempts(session: Session, ip: str) -> None:
        lockout = session.get(AuthLockout, ip)
        if lockout:
            session.delete(lockout)
            session.commit()

    @staticmethod
    async def is_locked_out_async(
        session: AsyncSession, ip: str, max_attempts: int = 5, lockout_duration: float = 300
    ) -> bool:
        lockout = await session.get(AuthLockout, ip)
        if not lockout:
            return False
        if lockout.attempts >= max_attempts:
            if time.time() - lockout.first_attempt_time < lockout_duration:
                return True
            await session.delete(lockout)
            await session.commit()
        return False

    @staticmethod
    async def record_failed_attempt_async(session: AsyncSession, ip: str) -> None:
        lockout = await session.get(AuthLockout, ip)
        now = time.time()
        if not lockout:
            lockout = AuthLockout(ip_address=ip, attempts=1, first_attempt_time=now)
            session.add(lockout)
        else:
            if now - lockout.first_attempt_time > 300:
                lockout.attempts = 1
                lockout.first_attempt_time = now
            else:
                lockout.attempts += 1
        await session.commit()

    @staticmethod
    async def clear_failed_attempts_async(session: AsyncSession, ip: str) -> None:
        lockout = await session.get(AuthLockout, ip)
        if lockout:
            await session.delete(lockout)
            await session.commit()


class ApiKey(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    label: str = Field(default="")
    key_hash: str = Field()
    is_active: bool = Field(default=True)
    created_at: float = Field(default_factory=time.time)

    @staticmethod
    async def has_any_key_async(session: AsyncSession) -> bool:
        result = await session.exec(select(ApiKey).where(ApiKey.is_active == True))  # noqa: E712
        return result.first() is not None

    @staticmethod
    def has_any_key(session: Session) -> bool:
        return session.exec(select(ApiKey).where(ApiKey.is_active == True)).first() is not None  # noqa: E712

    @staticmethod
    def store_key(session: Session, raw_key: str, label: str = "") -> "ApiKey":
        key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()
        obj = ApiKey(label=label, key_hash=key_hash)
        session.add(obj)
        session.commit()
        session.refresh(obj)
        return obj

    @staticmethod
    async def store_key_async(session: AsyncSession, raw_key: str, label: str = "") -> "ApiKey":
        key_hash = (
            await asyncio.to_thread(bcrypt.hashpw, raw_key.encode(), bcrypt.gensalt(rounds=12))
        ).decode()
        obj = ApiKey(label=label, key_hash=key_hash)
        session.add(obj)
        await session.commit()
        await session.refresh(obj)
        return obj

    @staticmethod
    def verify_any(session: Session, provided_key: str) -> bool:
        for row in session.exec(select(ApiKey).where(ApiKey.is_active == True)):  # noqa: E712
            try:
                if bcrypt.checkpw(provided_key.encode(), row.key_hash.encode()):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    async def verify_any_async(session: AsyncSession, provided_key: str) -> bool:
        result = await session.exec(select(ApiKey).where(ApiKey.is_active == True))  # noqa: E712
        provided = provided_key.encode()
        for row in result:
            try:
                if await asyncio.to_thread(bcrypt.checkpw, provided, row.key_hash.encode()):
                    return True
            except Exception:
                continue
        return False


def _migrate_proxmox_endpoint_columns() -> None:  # noqa: C901
    table = ProxmoxEndpoint.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    stmts: list[str] = []
    if "timeout" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN timeout INTEGER")
    if "max_retries" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN max_retries INTEGER")
    if "retry_backoff" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN retry_backoff REAL")
    if "allow_writes" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN allow_writes BOOLEAN NOT NULL DEFAULT 0")
    if "site_id" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN site_id INTEGER")
    if "site_slug" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN site_slug VARCHAR")
    if "site_name" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN site_name VARCHAR")
    if "tenant_id" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN tenant_id INTEGER")
    if "tenant_slug" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN tenant_slug VARCHAR")
    if "tenant_name" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN tenant_name VARCHAR")
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _migrate_netbox_endpoint_columns() -> None:
    table = NetBoxEndpoint.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    stmts: list[str] = []
    if "token_version" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN token_version VARCHAR DEFAULT 'v1'")
    if "token_key" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN token_key VARCHAR")
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.execute(
            text(
                f"UPDATE {table} SET token_version = 'v1' "
                "WHERE token_version IS NULL OR TRIM(COALESCE(token_version, '')) = ''"
            )
        )


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_proxmox_endpoint_columns()
    _migrate_netbox_endpoint_columns()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


DatabaseSessionDep = Annotated[Session, Depends(get_session)]
AsyncDatabaseSessionDep = Annotated[AsyncSession, Depends(get_async_session)]
